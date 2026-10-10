"""Photocraft <-> ComfyUI bridge over Photocraft's desktop control channel (docs/control-protocol.md).

Pixels travel as PNG files in a shared folder (the control channel caps requests at 1 MiB);
commands travel as JSON lines over the loopback TCP socket.
"""
import json
import os
import socket
import uuid

import numpy as np
import torch
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
_LAST_TARGET = {}  # the last Get's target, for a Send whose `target` input is left unconnected


def _config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


class Photocraft:
    """One authenticated connection to a running `photocraft --control <port>`."""

    def __init__(self):
        c = _config()
        self.dir = os.path.expanduser(c["bridge_dir"])
        with open(os.path.expanduser(c["token_file"]), encoding="utf-8") as f:
            token = f.read().strip()
        try:
            self.sock = socket.create_connection((c.get("host", "127.0.0.1"), int(c.get("port", 7878))), timeout=60)
        except OSError as e:
            raise RuntimeError(f"Photocraft is not reachable. Start it with --control {c.get('port', 7878)} ({e})") from e
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

    def path(self, name):
        return os.path.join(self.dir, name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.sock.close()


def _load_plane(path, mode):
    """PNG -> float32 HxW(xC) in 0..1, honouring 16-bit files."""
    with Image.open(path) as im:
        if im.mode not in ("I;16", "I"):  # PIL clips 16-bit grey when converting, so keep it as is
            im = im.convert(mode)
        a = np.asarray(im)
    a = a.astype(np.float32) / (255.0 if a.dtype == np.uint8 else 65535.0)
    if mode == "RGB" and a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=2)
    return a


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
            },
        }

    @classmethod
    def IS_CHANGED(cls, **_):
        return float("nan")  # always re-read: the document lives outside ComfyUI

    def get(self, crop_to_selection, padding, multiple_of=16):
        m = multiple_of
        tag = uuid.uuid4().hex[:12]
        img_name, mask_name = f"pcb_{tag}_image.png", f"pcb_{tag}_mask.png"
        with Photocraft() as pc:
            ui = pc.call("ui.inspect")
            idx = ui["session"]["active"]
            if idx is None:
                raise RuntimeError("No document is open in Photocraft")
            doc = ui["document"]
            name = ui["session"]["documents"][idx]["name"]
            # Export a flattened duplicate: app.save on the original would rename it and mark it saved.
            dup = pc.run("image.duplicate", {"mergedOnly": True, "name": "comfy bridge"})["document"]
            pc.save_temp_doc(dup, img_name, idx)
            if doc["hasSelection"]:
                sel = pc.run("channel.duplicate", {"channel": "selection", "document": "new"})["document"]
                pc.save_temp_doc(sel, mask_name, idx)
            files = [pc.path(img_name)] + ([pc.path(mask_name)] if doc["hasSelection"] else [])

        try:
            image = _load_plane(files[0], "RGB")
            h, w = image.shape[:2]
            mask = _load_plane(files[1], "L") if len(files) > 1 else np.ones((h, w), np.float32)
        finally:
            for f in files:
                os.remove(f)

        x = y = 0
        bounds = doc.get("selectionBounds")
        if crop_to_selection and bounds and bounds[2] > 0 and bounds[3] > 0:
            sx, sy, sw, sh = bounds
            x, y = max(0, sx - padding), max(0, sy - padding)
            x1, y1 = min(w, sx + sw + padding), min(h, sy + sh + padding)
            x, x1 = _snap(x, x1, w, m)
            y, y1 = _snap(y, y1, h, m)
            image, mask = image[y:y1, x:x1], mask[y:y1, x:x1]

        # Whatever still isn't a multiple (a small document, an odd canvas): pad bottom/right, Send cuts it off.
        h, w = image.shape[:2]
        ph, pw = -(-h // m) * m, -(-w // m) * m
        image = np.pad(image, ((0, ph - h), (0, pw - w), (0, 0)), mode="edge")
        mask = np.pad(mask, ((0, ph - h), (0, pw - w)))
        target = {"doc": name, "index": idx, "x": x, "y": y, "w": w, "h": h, "pw": pw, "ph": ph}
        _LAST_TARGET["target"] = target
        # The document's notes (Note tool): never rendered into the image, so a natural prompt field.
        prompt = "\n".join(n["text"] for n in doc.get("notes", []) if n["text"].strip())
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
            },
        }

    def send(self, images, layer_name, target=None, mask=None):
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
            docs = pc.call("ui.inspect")["session"]
            dest, x, y = _destination(docs, target, fallback)
            open_docs = len(docs["documents"])
            for i, img in enumerate(images):
                arr = img.cpu().numpy()
                if mask is not None:
                    m = mask[min(i, mask.shape[0] - 1)].cpu().numpy()
                    if m.shape != arr.shape[:2]:
                        raise ValueError(f"mask {tuple(m.shape)} does not match image {arr.shape[:2]}")
                    arr = np.dstack([arr[..., :3], m])
                name = f"pcb_{uuid.uuid4().hex[:12]}_out.png"
                Image.fromarray((np.clip(arr, 0, 1) * 255).round().astype(np.uint8)).save(pc.path(name))
                try:
                    pc.call("app.open", {"path": name})
                finally:
                    os.remove(pc.path(name))
                src = open_docs  # app.open appends the new document at the end
                if dest is None:  # nothing to put it into: leave it open as its own document
                    open_docs += 1
                    continue
                layer = pc.run("document.inspect", {"document": src})["layers"][0]["id"]
                copied = pc.run("layer.copyToDocument", {"document": dest, "source": src, "layers": [layer], "offset": [x, y]})
                for lid in copied["layers"]:
                    pc.run("layer.setProps", {"layer": lid, "name": layer_name})
                pc.run("file.close", {"document": src})
            if dest is not None:
                pc.run("document.activate", {"document": dest})
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


def _destination(session, target, fallback=False):
    """(document index or None, x, y) for the target handed over by PhotocraftGetImage."""
    docs = session["documents"]
    if target is None:
        return session["active"], 0, 0
    i = target["index"]
    if not (i < len(docs) and docs[i]["name"] == target["doc"]):
        i = next((d["index"] for d in docs if d["name"] == target["doc"]), None)
        if i is None:
            if fallback:  # a remembered target whose document was closed: behave as if there were none
                return session["active"], 0, 0
            raise RuntimeError(f"Photocraft document \"{target['doc']}\" is no longer open")
    return i, target["x"], target["y"]


NODE_CLASS_MAPPINGS = {
    "PhotocraftGetImage": PhotocraftGetImage,
    "PhotocraftSendImage": PhotocraftSendImage,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PhotocraftGetImage": "Photocraft ▸ Get Image",
    "PhotocraftSendImage": "Photocraft ◂ Send Image",
}
