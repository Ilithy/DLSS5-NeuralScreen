"""Сборка релизного архива NeuralScreen v1.0.0: git-файлы + артефакты + runtime."""
import os
import subprocess
import zipfile

files = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
extra = [
    "NeuralScreen.vbs",
    "README.ru.md",
    "native/nvngx.dll",
    "native/nvngx_dlssnr.dll",
]
for root, _dirs, fs in os.walk("runtime"):
    for f in fs:
        extra.append(os.path.join(root, f))

seen = set()
uniq = []
for f in files + extra:
    norm = f.replace("\\", "/")
    if norm in seen:
        continue
    seen.add(norm)
    uniq.append(norm)

out = "neuralscreen-v1.0.0-full.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for f in uniq:
        if os.path.isfile(f):
            z.write(f, f)
        else:
            print("MISSING:", f)
print("entries:", len(uniq))
print("size:", os.path.getsize(out))
