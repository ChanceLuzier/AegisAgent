"""Generate a minimal 1024x1024 PNG icon for Tauri using only stdlib."""
import struct, zlib, os

W = H = 1024

def png_chunk(tag, data):
    c = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", c)

def make_png(w, h, pixels):
    raw = b""
    for row in pixels:
        raw += b"\x00" + bytes(row)
    compressed = zlib.compress(raw, 9)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    idat = png_chunk(b"IDAT", compressed)
    iend = png_chunk(b"IEND", b"")
    return sig + ihdr + idat + iend

# Build pixel rows: dark navy background with a glowing blue circle
import math
pixels = []
cx, cy, r = W // 2, H // 2, W * 0.42
for y in range(H):
    row = []
    for x in range(W):
        dx, dy = x - cx, y - cy
        dist = math.sqrt(dx*dx + dy*dy)
        if dist < r * 0.72:
            # Inner bright blue core
            t = 1.0 - dist / (r * 0.72)
            rv = int(30 + t * 80)
            gv = int(80 + t * 120)
            bv = int(200 + t * 55)
            row += [min(rv,255), min(gv,255), min(bv,255)]
        elif dist < r:
            # Soft glow ring
            t = 1.0 - (dist - r * 0.72) / (r * 0.28)
            t = t * t
            rv = int(10 + t * 30)
            gv = int(20 + t * 60)
            bv = int(60 + t * 140)
            row += [min(rv,255), min(gv,255), min(bv,255)]
        else:
            # Dark bg with slight vignette
            t = max(0.0, 1.0 - (dist - r) / (r * 0.3))
            v = int(8 + t * 10)
            row += [v, v, v + 4]
    pixels.append(row)

out = "src-tauri/icons/source.png"
os.makedirs("src-tauri/icons", exist_ok=True)
with open(out, "wb") as f:
    f.write(make_png(W, H, pixels))
print(f"Written {out}")
