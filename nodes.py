"""Photocraft <-> ComfyUI bridge over Photocraft's desktop control channel (docs/control-protocol.md).

Pixels travel as PNG files in a shared folder (the control channel caps requests at 1 MiB);
commands travel as JSON lines over the loopback TCP socket.
"""
import io
import json
import os
import re
import socket
import threading
import time
import uuid

import numpy as np
import torch
from PIL import Image

try:
    from PIL import ImageCms

    _SRGB = ImageCms.createProfile("sRGB")
except ImportError:  # Pillow built without LittleCMS: documents are read without colour management
    ImageCms = None

try:  # inside ComfyUI: lets a waiting Get honour the Cancel button
    from comfy.model_management import throw_exception_if_processing_interrupted
except ImportError:
    def throw_exception_if_processing_interrupted():
        pass

README = "https://github.com/the-aSak/ComfyUI-Photocraft-Bridge#installation"
DEBOUNCE = 2.0  # seconds the notes must rest after typing before a generation starts

HERE = os.path.dirname(os.path.abspath(__file__))
_LAST_TARGET = {}  # the last Get's target, for a Send whose `target` input is left unconnected


DEFAULTS = {"port": 7878, "token_file": "~/photocraft-bridge/control.token", "bridge_dir": "~/photocraft-bridge"}


def _config():
    """The defaults, overridden by an optional config.json (git-ignored, so updates never touch it)."""
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        return DEFAULTS
    with open(path, encoding="utf-8") as f:
        return {**DEFAULTS, **json.load(f)}


def _launch_hint():
    """The exact command that starts PhotoCraft with the bridge enabled on this machine."""
    c = _config()
    folder, token = os.path.expanduser(c["bridge_dir"]), os.path.expanduser(c["token_file"])
    exe = '"C:\\Program Files\\PhotoCraft\\photocraft.exe"' if os.name == "nt" else "photocraft"
    cmd = f'{exe} --control {c["port"]} --control-token-file "{token}" --automation-read-root "{folder}" --automation-write-root "{folder}"'
    return f"Start PhotoCraft with the bridge enabled:\n  {cmd}\nSetup guide: {README}"


class Photocraft:
    """One authenticated connection to a running `photocraft --control <port>`."""

    def __init__(self):
        c = _config()
        self.dir = os.path.expanduser(c["bridge_dir"])
        token_file = os.path.expanduser(c["token_file"])
        try:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except FileNotFoundError:
            raise RuntimeError(f"No token file at {token_file} yet. {_launch_hint()}") from None
        try:
            # Loopback only: PhotoCraft listens nowhere else, and the token travels unencrypted.
            # 90 s: longer than PhotoCraft's own 60 s reply deadline, so its error message arrives first.
            self.sock = socket.create_connection(("127.0.0.1", int(c.get("port", 7878))), timeout=90)
        except OSError as e:
            raise RuntimeError(f"PhotoCraft is not reachable on port {c['port']} ({e}). {_launch_hint()}") from e
        self.f = self.sock.makefile("rw", encoding="utf-8", newline="\n")
        self.n = 0
        self.call("auth", {"token": token})

    def call(self, method, params=None):
        self.n += 1
        self.f.write(json.dumps({"id": self.n, "method": method, "params": params or {}}) + "\n")
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                raise RuntimeError(f"Photocraft closed the connection during {method} (wrong token?)")
            reply = json.loads(line)
            if reply.get("id") == self.n:
                break
        if not reply.get("ok"):
            raise RuntimeError(f"Photocraft {method}: {reply.get('error')}")
        return reply.get("result")

    def run(self, command, params=None):
        return self.call("engine.execute", {"command": command, "params": params or {}})

    def save_temp_doc(self, temp, name, back_to):
        """Export the temporary document `temp` (already active) to `name`, close it, reactivate `back_to`."""
        try:
            self.call("app.save", {"path": name})
        finally:
            self.run("file.close", {"document": temp})
            self.run("document.activate", {"document": back_to})
        if not os.path.exists(self.path(name)):
            raise RuntimeError(f"PhotoCraft saved {name}, but not into {self.dir}: bridge_dir must be the folder given as --automation-write-root")

    def forget_temp_files(self):
        """app.open lists each file it opens under File > Open Recent; drop the bridge's temporary ones."""
        try:
            recent = self.run("prefs.get", {"path": "fileHandling.recentFiles"}) or []
            kept = [p for p in recent if not re.fullmatch(r"pcb_[0-9a-f]{12}_out\.png", p)]
            if kept != recent:
                self.run("prefs.set", {"path": "fileHandling.recentFiles", "value": kept})
        except Exception as e:  # runs in a finally: never hide the error that got us here
            print(f"[Photocraft] could not tidy Open Recent: {e}")

    def path(self, name):
        return os.path.join(self.dir, name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.sock.close()


def _load_plane(path, mode):
    """PNG -> float32 HxW(xC) in 0..1, honouring 16-bit files and (for RGB) the embedded colour profile."""
    with Image.open(path) as im:
        icc = im.info.get("icc_profile")
        if not im.mode.startswith("I"):  # PIL clips 16-bit grey when converting, so keep it as is
            if mode == "RGB" and (im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info):
                # Transparent areas over white, as PhotoCraft flattens for formats without alpha.
                im = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im.convert("RGBA"))
            im = im.convert(mode)
            if mode == "RGB" and icc:
                im = _to_srgb(im, icc)
        a = np.asarray(im)
    a = a.astype(np.float32) / (255.0 if a.dtype == np.uint8 else 65535.0)
    if mode == "RGB" and a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=2)
    return a


def _notes_text(notes):
    """The prompt: the document's non-empty notes, line by line."""
    return "\n".join(n["text"] for n in notes if n["text"].strip())


def _current_notes(pc):
    try:
        return _notes_text(pc.run("notes.list")["notes"])
    except RuntimeError as e:
        if "no document open" in str(e):
            return ""
        raise


_LAST_RUN = {}  # the notes text the last note-triggered generation used


def _wait_for_new_notes(pc):
    """Block until the notes differ from the last generation's and have rested DEBOUNCE seconds."""
    seen, since = None, 0.0
    print("[Photocraft] waiting for the notes in PhotoCraft to change ...")
    while True:
        throw_exception_if_processing_interrupted()  # ComfyUI's Cancel button
        text, now = _current_notes(pc), time.monotonic()
        if text != seen:
            seen, since = text, now
        elif text and text != _LAST_RUN.get("prompt") and now - since >= DEBOUNCE:
            _LAST_RUN["prompt"] = text
            print("[Photocraft] notes changed: generating")
            return
        time.sleep(0.3)


def _cancel_when_notes_change(prompt):
    """Watch the notes while this generation runs; if they change, cancel it so the next one starts."""
    try:
        from server import PromptServer

        pid, queue = PromptServer.instance.last_prompt_id, PromptServer.instance.prompt_queue
    except Exception:  # not running inside ComfyUI
        return

    def running():
        return any(item[1] == pid for item in queue.get_current_queue_volatile()[0])

    def watch():
        try:
            with Photocraft() as pc:
                while running():
                    if _current_notes(pc) != prompt:
                        print("[Photocraft] notes changed: cancelling the running generation")
                        queue.interrupt_if_running(pid)  # only ever this run, never a later one
                        return
                    time.sleep(0.5)
        except Exception as e:
            print(f"[Photocraft] stopped watching the notes: {e}")

    threading.Thread(target=watch, daemon=True).start()


def _to_srgb(im, icc):
    """Document profile (Adobe RGB, Display P3, ...) -> sRGB, the space models expect. Send's untagged
    PNG reads back as sRGB, and PhotoCraft converts it into the document's profile again."""
    if ImageCms is None:
        print("[Photocraft] Pillow has no ImageCms: colour profile ignored")
        return im
    try:
        return ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)), _SRGB, outputMode="RGB")
    except (ImageCms.PyCMSError, OSError) as e:
        print(f"[Photocraft] colour profile ignored: {e}")
        return im


class PhotocraftGetImage:
    """Like a subgraph input: the active Photocraft document (flattened), its selection as mask, its size."""

    CATEGORY = "Photocraft"
    FUNCTION = "get"
    RETURN_TYPES = ("IMAGE", "MASK", "INT", "INT", "PHOTOCRAFT_TARGET", "STRING")
    RETURN_NAMES = ("image", "mask", "width", "height", "target", "prompt")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "crop_to_selection": ("BOOLEAN", {"default": False}),
                "padding": ("INT", {"default": 32, "min": 0, "max": 4096, "step": 8}),
            },
            # Optional so workflows saved before it existed still run (with the default).
            "optional": {
                # VAE Encode centre-crops sizes that aren't multiples of 8 (Flux-type models: 16),
                # which shifts the result against the document. Hand out sizes that need no crop.
                "multiple_of": ("INT", {"default": 16, "min": 1, "max": 128}),
                # "active layer": only that layer (transparent areas become white), like "Sample: Current Layer".
                "source": (["composite", "active layer"], {"default": "composite"}),
                # "note change": wait until the notes change, then run; a change during a run cancels it.
                # Meant for the Run button's "Run (Instant)" mode: generate while you type.
                "trigger": (["queue", "note change"], {"default": "queue"}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **_):
        return float("nan")  # always re-read: the document lives outside ComfyUI

    def get(self, crop_to_selection, padding, multiple_of=16, source="composite", trigger="queue"):
        m = multiple_of
        tag = uuid.uuid4().hex[:12]
        img_name, mask_name = f"pcb_{tag}_image.png", f"pcb_{tag}_mask.png"
        bridge = os.path.expanduser(_config()["bridge_dir"])
        files = [os.path.join(bridge, img_name), os.path.join(bridge, mask_name)]
        try:
            with Photocraft() as pc:
                if trigger == "note change":
                    _wait_for_new_notes(pc)
                ui = pc.call("ui.inspect")
                idx = ui["session"]["active"]
                if idx is None:
                    raise RuntimeError("No document is open in Photocraft")
                doc = ui["document"]
                name = ui["session"]["documents"][idx]["name"]
                # Export a temporary document: app.save on the original would rename it and mark it saved.
                if source == "active layer":
                    if doc["activeLayer"] is None:
                        raise RuntimeError("The document has no active layer")
                    dup = pc.run("file.new", {"width": doc["width"], "height": doc["height"], "background": "transparent"})["document"]
                    pc.run("layer.copyToDocument", {"document": dup, "source": idx, "layers": [doc["activeLayer"]]})
                else:
                    dup = pc.run("image.duplicate", {"mergedOnly": True, "name": "comfy bridge"})["document"]
                pc.save_temp_doc(dup, img_name, idx)
                # In Quick Mask mode (Q) the selection is being painted and is empty until you leave it.
                channel = "quickMask" if doc.get("quickMask") else "selection" if doc["hasSelection"] else None
                if channel:
                    sel = pc.run("channel.duplicate", {"channel": channel, "document": "new"})["document"]
                    pc.save_temp_doc(sel, mask_name, idx)
            image = _load_plane(files[0], "RGB")
            h, w = image.shape[:2]
            mask = _load_plane(files[1], "L") if channel else np.ones((h, w), np.float32)
        finally:
            for f in files:
                if os.path.exists(f):
                    os.remove(f)

        x = y = 0
        if crop_to_selection:
            ys, xs = np.nonzero(mask > 0.5 / 255)  # the mask's own bounds: selection and Quick Mask alike
            if not len(xs):
                raise RuntimeError("The selection is empty (or lies outside the canvas); nothing to crop to")
            x, y = max(0, int(xs.min()) - padding), max(0, int(ys.min()) - padding)
            x1, y1 = min(w, int(xs.max()) + 1 + padding), min(h, int(ys.max()) + 1 + padding)
            x, x1 = _snap(x, x1, w, m)
            y, y1 = _snap(y, y1, h, m)
            image, mask = image[y:y1, x:x1], mask[y:y1, x:x1]

        # Whatever still isn't a multiple (a small document, an odd canvas): pad bottom/right, Send cuts it off.
        h, w = image.shape[:2]
        ph, pw = -(-h // m) * m, -(-w // m) * m
        image = np.pad(image, ((0, ph - h), (0, pw - w), (0, 0)), mode="edge")
        mask = np.pad(mask, ((0, ph - h), (0, pw - w)))
        # Layer ids are unique across a PhotoCraft session, so they find the document again even after
        # Save As renamed it or the tabs were reordered.
        layers = [layer["id"] for layer in doc["layers"]]
        target = {"doc": name, "index": idx, "layers": layers, "x": x, "y": y, "w": w, "h": h, "pw": pw, "ph": ph}
        _LAST_TARGET["target"] = target
        # The document's notes (Note tool): never rendered into the image, so a natural prompt field.
        prompt = _notes_text(doc.get("notes", []))
        if trigger == "note change":
            _cancel_when_notes_change(prompt)
        return (torch.from_numpy(image)[None], torch.from_numpy(mask)[None], pw, ph, target, prompt)


class PhotocraftSendImage:
    """Like a subgraph output: each image becomes a new layer in the target document (or the active one)."""

    CATEGORY = "Photocraft"
    FUNCTION = "send"
    RETURN_TYPES = ()
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "layer_name": ("STRING", {"default": "ComfyUI"}),
            },
            "optional": {
                "target": ("PHOTOCRAFT_TARGET",),
                "mask": ("MASK",),
                # With a mask: put it into a layer mask instead of the pixels' alpha, so it can be repainted
                # and the whole generated image stays underneath.
                "layer_mask": ("BOOLEAN", {"default": False}),
            },
        }

    def send(self, images, layer_name, target=None, mask=None, layer_mask=False):
        images = images.float()  # some nodes hand out bf16/fp16, which numpy can't take
        if mask is not None:
            mask = (mask[None] if mask.dim() == 2 else mask).float()
        fallback = target is None
        target = target or _LAST_TARGET.get("target")
        if target:
            # Edit models often change the resolution; scale back to what Get handed out, then drop its padding.
            h, w = target["h"], target["w"]
            ph, pw = target.get("ph", h), target.get("pw", w)
            if tuple(images.shape[1:3]) != (ph, pw):
                print(f"[Photocraft] result {images.shape[2]}x{images.shape[1]} scaled to {pw}x{ph} (Get's size)")
            images = _resize(images, ph, pw)[:, :h, :w]
            if mask is not None:
                mask = _resize(mask[..., None], ph, pw)[:, :h, :w, 0]
        with Photocraft() as pc:
            try:
                session = pc.call("ui.inspect")["session"]
                dest, x, y = _destination(pc, session, target, fallback)
                for i, img in enumerate(images):
                    arr = img.cpu().numpy()
                    alpha = np.ones(arr.shape[:2], np.float32)
                    if mask is not None:
                        alpha = mask[min(i, mask.shape[0] - 1)].cpu().numpy()
                        if alpha.shape != arr.shape[:2]:
                            raise ValueError(f"mask {tuple(alpha.shape)} does not match image {arr.shape[:2]}")
                    # Always RGBA: an opaque PNG opens as a Background layer, which loses its name when copied.
                    arr = np.dstack([arr[..., :3], alpha])
                    if dest is None:  # no document open: make one the size of the result
                        dest = pc.run("file.new", {"width": arr.shape[1], "height": arr.shape[0], "background": "transparent"})["document"]
                    name = f"pcb_{uuid.uuid4().hex[:12]}_out.png"
                    Image.fromarray((np.clip(arr, 0, 1) * 255).round().astype(np.uint8)).save(pc.path(name))
                    try:
                        pc.call("app.open", {"path": name})
                    finally:
                        os.remove(pc.path(name))
                    src = pc.call("ui.inspect")["session"]["active"]
                    try:
                        layer = pc.run("document.inspect", {"document": src})["layers"][0]["id"]
                        # Named and masked here, the copy keeps both: one undo step in the document.
                        if layer_name.strip():
                            pc.run("layer.setProps", {"layer": layer, "name": layer_name})
                        if layer_mask and mask is not None:
                            pc.run("layer.layerMask.fromTransparency", {"layer": layer})
                        pc.run("layer.copyToDocument", {"document": dest, "source": src, "layers": [layer], "offset": [x, y]})
                    finally:
                        pc.run("file.close", {"document": src})
                pc.run("document.activate", {"document": dest})
            finally:
                pc.forget_temp_files()
        return {}


def _snap(a, b, size, m):
    """Grow [a, b) to a length that is a multiple of m, staying within [0, size)."""
    n = min(-(-(b - a) // m) * m, size)
    b = min(a + n, size)
    return b - n, b


assert _snap(40, 120, 200, 16) == (40, 120) and _snap(30, 80, 150, 16) == (30, 94)
assert _snap(190, 200, 200, 16) == (184, 200) and _snap(0, 150, 150, 16) == (0, 150)


def _resize(t, h, w):
    """[B,H,W,C] tensor to h x w (no-op when it already fits)."""
    if t.shape[1:3] == (h, w):
        return t
    out = torch.nn.functional.interpolate(t.movedim(-1, 1).float(), size=(h, w), mode="bicubic", antialias=True)
    return out.movedim(1, -1).clamp(0, 1)


def _destination(pc, session, target, fallback=False):
    """(document index or None, x, y) for the target handed over by PhotocraftGetImage."""
    if target is None:
        return session["active"], 0, 0
    wanted = set(target.get("layers", []))
    n = len(session["documents"])
    for i in sorted(range(n), key=lambda i: i != target["index"]):  # the remembered index first
        if wanted & {layer["id"] for layer in pc.run("document.inspect", {"document": i})["layers"]}:
            return i, target["x"], target["y"]
    if fallback:  # a remembered target whose document was closed: behave as if there were none
        print(f"[Photocraft] \"{target['doc']}\" is no longer open; sending to the active document")
        return session["active"], 0, 0
    raise RuntimeError(f"Photocraft document \"{target['doc']}\" is no longer open")


def _startup_check():
    """Installed through ComfyUI-Manager, nobody has read the README yet: say what's missing."""
    c = _config()
    os.makedirs(os.path.expanduser(c["bridge_dir"]), exist_ok=True)  # PhotoCraft needs it before it starts
    if not os.path.exists(os.path.expanduser(c["token_file"])):
        print(f"[Photocraft Bridge] Not connected to PhotoCraft yet. {_launch_hint()}")


try:
    _startup_check()
except Exception as e:  # never keep ComfyUI from starting
    print(f"[Photocraft Bridge] {e}")


NODE_CLASS_MAPPINGS = {
    "PhotocraftGetImage": PhotocraftGetImage,
    "PhotocraftSendImage": PhotocraftSendImage,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PhotocraftGetImage": "Photocraft ▸ Get Image",
    "PhotocraftSendImage": "Photocraft ◂ Send Image",
}
