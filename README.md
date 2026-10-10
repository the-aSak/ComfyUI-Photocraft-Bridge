# ComfyUI ⇄ PhotoCraft Bridge

Two ComfyUI nodes that connect a ComfyUI workflow to [PhotoCraft](https://github.com/storytold/photocraft), the open-source Photoshop reimplementation in Rust. They work like a subgraph's input and output:

- **Photocraft ▸ Get Image** reads the active PhotoCraft document (flattened), its selection as a `MASK`, the width and height, a `target` (which document, and where) and a `prompt` taken from the document's notes.
- **Photocraft ◂ Send Image** puts the result back as a new layer in that document, at the right position.

A typical round trip: draw a selection in PhotoCraft, write a prompt into a note, press Queue in ComfyUI, and the inpainted result appears as a layer over the selection.

## How it works

There is no PhotoCraft plugin. PhotoCraft's plugins are sandboxed WebAssembly filters without network or file access, so they can't talk to ComfyUI. Instead the nodes use PhotoCraft's built-in control channel (see `docs/control-protocol.md` in the PhotoCraft repo): commands go as JSON lines over `127.0.0.1`, pixels go as PNG files through a shared folder.

Get never modifies your document: it exports a flattened duplicate and a copy of the selection, then closes both. Send adds the new layer and its name as two undo steps per image.

## Installation

1. Copy or `git clone` this folder into `ComfyUI/custom_nodes/`. 
2. Create an exchange folder, e.g. `~/photocraft-bridge` (Windows: `C:\Users\<Profile>\photocraft-bridge`).
3. Start PhotoCraft with the control channel enabled and both automation roots set to that folder. PhotoCraft creates the token file on first start.

   Windows, Command Prompt (`cmd.exe`):

   ```bat
   "C:\Program Files\PhotoCraft\photocraft.exe" --control 7878 --control-token-file "%USERPROFILE%\photocraft-bridge\control.token" --automation-read-root "%USERPROFILE%\photocraft-bridge" --automation-write-root "%USERPROFILE%\photocraft-bridge"
   ```

   Windows, PowerShell: the same, but with `$env:USERPROFILE` instead of `%USERPROFILE%`.

   Linux / macOS:

   ```sh
   photocraft --control 7878 \
     --control-token-file ~/photocraft-bridge/control.token \
     --automation-read-root ~/photocraft-bridge \
     --automation-write-root ~/photocraft-bridge
   ```

   Running PhotoCraft from source? Put the arguments after `--`, otherwise Cargo tries to read them itself:

   ```sh
   cargo run --release -p photocraft -- --control 7878 --control-token-file ...
   ```

4. If you want to use a different port or folder, edit `config.json` in this folder. `bridge_dir` must be the same folder as the automation roots. `~` stands for your home folder, on Windows too.
5. Restart ComfyUI. The nodes are in the `Photocraft` category.

## Usage

**Whole image** (img2img, upscaling, style transfer): connect Get's `image` to your workflow and its result to Send. Connect Get's `target` to Send. Press Queue; the result appears as a layer named `ComfyUI` above the active layer.

**Inpainting part of the image:** Create a selection in PhotoCraft. In Get, turn on `crop_to_selection` and set `padding` for the context around the selection. Get outputs only that area plus the mask, and `target` remembers where it came from. Connect the `mask` to Send as well, so only the masked part replaces the original: the mask becomes the new layer's alpha.

**Prompt from PhotoCraft:** select the Note tool (in the Eyedropper tool group, right-click the eyedropper icon), click anywhere on the canvas and type the prompt into the Notes panel. Notes are never rendered into the image. Get outputs their text as `prompt`; connect it to your text encoder's `text` input. Several notes are joined line by line, empty ones skipped.

`width` and `height` are the size of what Get outputs, e.g. for `Empty Latent Image`.

Get re-reads the document on every Queue (no caching), so changes in PhotoCraft are always picked up.

### Pixel-exact placement (`multiple_of`, default 16)

`VAE Encode` centre-crops images whose sides aren't multiples of 8, and Flux-type models (Flux, Krea) want multiples of 16. The result would then sit a few pixels off. So Get grows the cropped area to multiples of `multiple_of`, and where that isn't possible (canvas edge, an odd document size) pads right/bottom with edge pixels. Send cuts the padding off again. For SD 1.5 / SDXL, 8 is enough.

If the workflow resizes on its own (e.g. to a fixed megapixel count), Send scales the result back and says so in the ComfyUI console (`[Photocraft] result … scaled to …`). If that resize changes the aspect ratio or crops edges, the result won't line up exactly; make the resize node round to multiples of 16, or drop it.

### Without `target`

If Send's `target` isn't connected, it uses the target of the last Get run, as long as that document is still open. Otherwise the result goes into the active document at (0, 0), or opens as a new document if none is open.

## Limits

- The workflow always starts from ComfyUI (Queue). A button inside PhotoCraft isn't possible without network access for plugins.
- Images travel as 8-bit PNG. 16-bit documents come back as 8-bit layers.
- Get exports the visible, flattened composite, not a single layer.
- PhotoCraft calls itself early alpha; command names may change. Tested against `storytold/photocraft` as of 10 October 2026.
- Anyone with the token can fully control PhotoCraft. Keep the token file private and never expose the port beyond your machine.

## Credits and license

Written by Claude (Anthropic), commissioned and tested by [the-aSak](https://github.com/the-aSak/).

Released into the public domain under [The Unlicense](LICENSE).
