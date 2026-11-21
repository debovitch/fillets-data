#!/usr/bin/env python3
"""Replay the reviewed FF4E still-image import. Requires Pillow with WebP support."""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "492dfd02a9f413e00a27b40d22d960732e931f78"
BASE = "7daaf56dc85500135588037c0bbbb0870b92ec6d"
CATALOG = ROOT / "docs/ff4e-upscales.json"
AI = Path("public/enhanced-ai")
STAGED = Path("public/enhanced")
MENU = Path("public/data/Menu")
QUARANTINE = Path("quarantine/ff4e")


def check(condition, message):
    if not condition:
        raise ValueError(str(message))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dimensions(im):
    return list(im.size)


def alpha(im):
    return list(im.getchannel("A").getextrema()) if im.mode == "RGBA" else [255, 255]


def visible(im):
    return im.convert("RGBA").convert("RGBa")


def room_names(src):
    names = {}
    mapping = src / "test/fixtures/solutions/mapping.tsv"
    for row in mapping.read_text().splitlines()[1:]:
        slug, _, aliases, *_ = row.split("\t")
        names.update({name.strip(): slug for name in aliases.split("|") if name.strip()})
    ts = (src / "tools/lib/ffngCodename.ts").read_text()
    for table in ("MISSING", "WRONG"):
        block = ts.split("const CODENAME_" + table, 1)[1].split("};", 1)[0]
        for name, slug in re.findall(r"(\w+): '(\w+)'", block):
            if table == "WRONG" or name not in names:
                names[name] = slug
    return names


def room_plans(src, folder, slug):
    name = folder.name
    ai = read(folder / "ai.json")
    check(ai["scale"] == 4, folder)
    models = (ROOT / "script" / slug / "models.lua").read_text()
    originals = ROOT / "images" / slug
    by_hash = defaultdict(list)
    for png in originals.glob("*.png"):
        by_hash[sha(png.read_bytes())].append(png)
    plans = {}
    for field, pattern, kind in [
        ("bg", r'createRoom\([^)]*/([\w-]+\.png)"', "background"),
        ("wall", r'addItemAnim\(\s*room\b[^)]*/([\w-]+\.png)"', "wall"),
    ]:
        first = re.search(pattern, models).group(1)
        for frame, key in enumerate(ai[field]):
            master = STAGED / name / Path(key).with_suffix(".png")
            matches = by_hash[sha((src / master).read_bytes())]
            check(len(matches) == 1 and (frame or matches[0].name == first), master)
            plans[key] = dict(kind=kind, original=matches[0], master=master,
                              method="lua-layer+identical-png", frame=frame)
    objfile = folder / "objects.json"
    objects = read(objfile)["objects"] if objfile.exists() else []
    if objects:
        check(objects == read(src / STAGED / name / "objects.json")["objects"], objfile)
    entries = []
    for obj in objects:
        frames = ["obj/" + str(Path(f).with_suffix(".webp")) for f in obj["frames"]]
        entries.append(dict(item=obj["item"], frames=frames))
        for frame, (file, key) in enumerate(zip(obj["frames"], frames)):
            master = STAGED / name / "obj" / file
            check(originals / file in by_hash[sha((src / master).read_bytes())], master)
            plan = plans.setdefault(key, dict(kind="sprite", original=originals / file,
                                    master=master, method="item-manifests+identical-png", bindings=[]))
            plan["bindings"].append([obj["item"], frame])
    check(entries == ai["objects"], folder / "ai.json")
    return plans


def shared_plans(src, folder):
    name = folder.name
    if name == "_fish":
        plans = {}
        native = read(src / STAGED / name / "manifest.json")
        for size, facings in read(folder / "x4/manifest.json").items():
            for facing, files in facings.items():
                check([Path(f).with_suffix(".png").name for f in files] == native[size][facing], files)
                for file in files:
                    png = Path(file).with_suffix(".png")
                    body = ROOT / "images/fishes" / size
                    part = body / "heads" if file.startswith("head_") else body
                    master = STAGED / name / size / facing / png
                    original = part / facing / png
                    check(original.read_bytes() == (src / master).read_bytes(), master)
                    plans[f"x4/{size}/{facing}/{file}"] = dict(kind="fish", original=original,
                        master=master, method="fish-manifests+identical-png")
        return plans
    ai = read(folder / "ai.json")
    check(ai["scale"] == 4, folder)
    if name == "_kufr":
        native = read(src / STAGED / name / "frames.json")
        check(ai["order"] == [str(Path(f).with_suffix(".webp")) for f in native["order"]], folder)
        check(ai["region"] == native["region"] and ai["base"] == native["base"], folder)
        plans = {"base.webp": dict(kind="cutscene-base", original=ROOT / "images/demo_briefcase/kufr256.png",
                 master=STAGED / name / "base.png", method="cutscene-lua+visible-pixels")}
        for f in ai["frames"]:
            positions = [i for i, entry in enumerate(ai["order"]) if entry == f]
            plan = dict(kind="cutscene-frame", playback=positions, logical=[ai["region"]["w"], ai["region"]["h"]])
            if positions == [0] and f == "f0000.webp":
                plan.update(original=ROOT / "images/demo_briefcase/demo_000.png", method="playback+opaque-first-frame+visual")
            else:
                plan["reason"] = "opaque-composite-vs-transparent-delta" if positions else "absent-from-playback-order"
            plans["frames/" + f] = plan
        check(set(ai["order"]) - set(ai["frames"]) == set(ai["original"]), folder)
        return plans
    if name == "_desky":
        placements = read(folder / "plaques.json")["plaques"]
        check(set(placements) == {str(Path(f).with_suffix(".png")) for f in ai["files"]}, name)
    plans = {}
    for file in ai["files"]:
        master = STAGED / name / Path(file).with_suffix(".png")
        plan = dict(master=master, kind=name.lstrip("_"))
        if name == "_story":
            with Image.open(src / master) as im:
                pixels = visible(im).tobytes()
            matches = []
            for path in (ROOT / "images").glob("*/poster.png"):
                with Image.open(path) as poster:
                    if visible(poster).tobytes() == pixels:
                        matches.append(path)
            check(len(matches) == 1, master)
            plan.update(original=matches[0], method="unique-poster+visible-pixels", kind="poster")
        else:
            plan["reason"] = {"_desky": "baked-localized-map-plaque", "_panel": "separate-FF4E-control-panel",
                              "_credits": "different-credits-layout"}[name]
        plans[file] = plan
    return plans


def menu_plans():
    plans = {}
    for name in ["mapa-0", "mapa-1", "krokomer", "n0", "n1", "n2", "n3", "n4", "ikonky", "loading"]:
        key = name + ("_ai.png" if re.fullmatch(r"n\d", name) else "_ai.webp")
        plan = dict(kind="world-map", master=STAGED / "_menu" / (name + ".png"))
        if name in {"loading", "mapa-0", "ikonky"}:
            plan["reason"] = {"loading": "loading-parchment-without-PNG", "mapa-0": "different-map-art",
                              "ikonky": "separate-icon-atlas"}[name]
            if name == "loading":
                plan["master"] = MENU / "loading.BMP"
        else:
            png = {"mapa-1": "map_lower", "krokomer": "pedometer"}.get(name, name)
            plan.update(original=ROOT / "images/menu" / (png + ".png"), method="menu-layer+visible-pixels")
        plans[key] = plan
    return plans


def plans(src):
    names = room_names(src)
    for folder in sorted((src / AI).iterdir()):
        if not folder.is_dir():
            continue
        name = folder.name
        if name == "SCORE":
            group = {key: dict(kind=kind, reason="nearest-neighbour-SCORE")
                     for kind, field in [("background", "bg"), ("wall", "wall")]
                     for key in read(folder / "ai.json")[field]}
        elif name.startswith("_"):
            group = shared_plans(src, folder)
        else:
            group = room_plans(src, folder, names[name])
        actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.suffix.lower() in {".webp",".png",".jpg",".jpeg",".gif",".avif",".bmp"}}
        check(actual == set(group), f"{name}: inventory mismatch {actual ^ set(group)}")
        stampdir = folder / "x4" if name == "_fish" else folder
        stamps = {} if name == "SCORE" else read(stampdir / ".build-stamp.json")
        for key, plan in sorted(group.items()):
            path = folder / key
            stamp = stamps.get(str(path.relative_to(stampdir))) if stamps else None
            check(name == "SCORE" or stamp, path)
            yield path, names.get(name, "score" if name == "SCORE" else name), plan, stamp
    menu = src / MENU
    group = menu_plans()
    check({p.name for p in menu.glob("*_ai.*")} == set(group), menu)
    stamps = read(menu / ".build-stamp.json")
    for key, plan in sorted(group.items()):
        yield menu / key, "_menu", plan, stamps.get(key)


def output_bytes(path):
    if path.suffix == ".webp":
        return path.read_bytes()
    # Map node masters are shipped only as PNG. Keep RGBA, including hidden RGB.
    with Image.open(path) as im:
        out = io.BytesIO()
        im.save(out, "WEBP", lossless=True, exact=True, method=6)
        data = out.getvalue()
        with Image.open(io.BytesIO(data)) as encoded:
            check(encoded.convert("RGBA").tobytes() == im.convert("RGBA").tobytes(), path)
        return data


def inspect(src, path, level, plan, stamp):
    source = path.relative_to(src)
    model = stamp.split(":")[1] if stamp else ("orig" if level == "score" else "x4plus")
    original = plan.get("original")
    master = plan.get("master")
    reason = "nearest-neighbour" if model == "orig" else plan.get("reason")
    status = "excluded" if model == "orig" else ("quarantine" if reason else "integrated")
    dest = None
    if status == "integrated":
        dest = original.relative_to(ROOT).with_suffix(".webp")
    elif status == "quarantine":
        dest = QUARANTINE / source
    r = dict(source=str(source), level=level, type=plan["kind"], status=status, model=model,
             original=str(original.relative_to(ROOT)) if original else None,
             destination=str(dest) if dest else None, method=plan.get("method", reason),
             confidence="high" if original else None)
    if reason:
        r["reason"] = plan.get("reason", reason)
    for key in ("bindings","frame","playback"):
        if key in plan:
            r[key] = plan[key]
    if master:
        r["master"] = str(master)
        if stamp:
            check(hashlib.md5((src / master).read_bytes()).hexdigest() == stamp.split(":")[0], master)
    with Image.open(path) as enhanced:
        check(enhanced.format == ("WEBP" if path.suffix == ".webp" else "PNG") and enhanced.n_frames == 1, path)
        enhanced.load()
        r["pixels"] = dimensions(enhanced)
        r["alpha"] = alpha(enhanced)
        if original:
            with Image.open(original) as im:
                r["logical"] = dimensions(im)
                r["original_alpha"] = alpha(im.convert("RGBA"))
                r["original_sha256"] = sha(original.read_bytes())
                if master and "visible-pixels" in r["method"]:
                    with Image.open(src / master) as staged:
                        check(im.size == staged.size and visible(im).tobytes() == visible(staged).tobytes(), original)
                check(r["original_alpha"][0] == 255 or r["alpha"][0] < 255, path)
                check((r["original_alpha"][1] == 0) == (r["alpha"][1] == 0), path)
                # Compare the visible picture at the same scale on a neutral backing.
                def preview(image):
                    small = image.convert("RGBA").resize((64, 48), Image.Resampling.BOX)
                    back = Image.new("RGBA", small.size, (96, 96, 96, 255))
                    return Image.alpha_composite(back, small).convert("RGB")
                diff = ImageChops.difference(preview(im), preview(enhanced))
                r["preview_mae"] = round(sum(ImageStat.Stat(diff).mean) / 3, 3)
        elif master:
            with Image.open(src / master) as im:
                r["logical"] = dimensions(im)
        else:
            r["logical"] = plan.get("logical") or [v // 4 for v in enhanced.size]
    check(r["pixels"] == [4*v for v in r["logical"]], path)
    r["scale"] = 4
    r["source_sha256"] = sha(path.read_bytes())
    if dest:
        data = output_bytes(path)
        r.update(sha256=sha(data), bytes=len(data))
    return r


def inventory(src):
    revision = subprocess.check_output(["git", "-C", str(src), "rev-parse", "HEAD"], text=True).strip()
    check(revision == SOURCE, f"Expected FF4E revision {SOURCE}, found {revision}")
    return [inspect(src, *args) for args in plans(src)]


def publish(path, data):
    # Hard-link a completed file into place. A racing writer cannot be overwritten.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp = Path(tmp.name)
    try:
        temp.write_bytes(data)
        temp.chmod(0o644)
        try:
            os.link(temp, path)
        except FileExistsError:
            check(path.read_bytes() == data, f"Concurrent change at {path}")
        check(sha(path.read_bytes()) == sha(data), path)
    finally:
        temp.unlink()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("source", type=Path)
    mode = cli.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    cli.add_argument("--inventory", action="store_true", help="also emit the verified JSON inventory on stdout")
    args = cli.parse_args()
    src = args.source.resolve()
    manifest = read(CATALOG)
    check(manifest["source_commit"] == SOURCE, CATALOG)
    rows = inventory(src)
    check(len(rows) == len(manifest["assets"]), "Incomplete source inventory")
    pending = []
    seen = set()
    identical = 0
    for row, expected in zip(rows, manifest["assets"]):
        check(row == expected, f"Manifest mismatch for {row['source']}: " +
              ", ".join(k for k in row.keys() | expected.keys() if row.get(k) != expected.get(k)))
        if not row["destination"]:
            continue
        dest = ROOT / row["destination"]
        check(dest not in seen and dest.resolve() == dest, dest)
        seen.add(dest)
        if dest.exists():
            existing = sha(dest.read_bytes())
            if existing != row["sha256"]:
                with Image.open(dest) as im:
                    details = (im.format, im.size, im.mode)
                raise ValueError(f"Conflict at {dest}: existing {existing} {details}; "
                                 f"expected {row['sha256']} {row['pixels']} alpha {row['alpha']}")
            identical += 1
        else:
            pending.append((dest, row))
    if args.apply:
        for dest, row in pending:
            data = output_bytes(src / row["source"])
            check(sha(data) == row["sha256"], row["source"])
            publish(dest, data)
    action = "written" if args.apply else "would write"
    if args.inventory:
        json.dump({**manifest, "assets": rows}, sys.stdout, indent=2)
        print()
    print(f"{len(rows)} candidates; {len(pending)} {action}; {identical} already identical; "
          f"{len(rows)-len(seen)} excluded.", file=sys.stderr if args.inventory else sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Import failed: {exc}")
