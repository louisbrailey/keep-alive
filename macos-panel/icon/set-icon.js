// Usage: osascript -l JavaScript set-icon.js <image.png|.icns> <target path>
// Applies a custom Finder icon to a file, folder, or app bundle via NSWorkspace.
ObjC.import('Cocoa');
function run(argv) {
  const [imagePath, target] = argv;
  const img = $.NSImage.alloc.initWithContentsOfFile($(imagePath));
  if (img.isNil()) throw new Error('could not load image: ' + imagePath);
  const ok = $.NSWorkspace.sharedWorkspace.setIconForFileOptions(img, $(target), 0);
  return ok ? 'ok: ' + target : 'failed: ' + target;
}
