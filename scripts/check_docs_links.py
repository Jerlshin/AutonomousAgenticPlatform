"""Verify every intra-repo markdown link, using GitHub's actual slug algorithm."""
import re, pathlib, sys

# github-slugger's removal set, written with explicit escapes so no literal
# character can be mistyped into a catastrophic range.
PUNCT = re.compile(
    "[ -⁯⸀-⹿"
    "\\\\'!\"#$%&()*+,./:;<=>?@\\[\\]^`{|}~]"
)

def slug(h: str) -> str:
    h = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', h)   # links -> label
    h = re.sub(r'`([^`]*)`', r'\1', h)               # code spans
    h = re.sub(r'\*\*([^*]*)\*\*', r'\1', h)
    h = re.sub(r'\*([^*]*)\*', r'\1', h)
    return PUNCT.sub('', h).strip().lower().replace(' ', '-')

def headings(text):
    out, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r'^#{1,6}\s+(.*?)\s*$', line)
        if m:
            out.append(m.group(1))
    return out

strip_code = lambda t: re.sub(r'```.*?```', '', t, flags=re.S)

FILES = ["README.md", "notes.md",
         "docs/ARCHITECTURE.md", "docs/AGENTS.md", "docs/MLOPS.md", "docs/FRONTEND.md"]
root = pathlib.Path.cwd()

anchors, dupes = {}, []
for f in FILES:
    hs = [slug(h) for h in headings(pathlib.Path(f).read_text())]
    assert hs and any(hs), f"slug() produced nothing for {f} — checker is broken"
    anchors[f] = set(hs)
    d = {h for h in hs if hs.count(h) > 1}
    if d:
        dupes.append((f, d))

bad, total = [], 0
for f in FILES:
    base = pathlib.Path(f).parent
    for m in re.finditer(r'\[([^\]\n]*)\]\(([^)\s]+)\)',
                         strip_code(pathlib.Path(f).read_text())):
        label, target = m.groups()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        total += 1
        path_part, _, frag = target.partition('#')
        if path_part:
            resolved = (base / path_part).resolve()
            if not resolved.exists():
                bad.append((f, label, target, "FILE MISSING"))
                continue
            key = str(resolved.relative_to(root))
        else:
            key = f
        if frag and key in anchors and frag not in anchors[key]:
            bad.append((f, label, target, f"ANCHOR MISSING in {key}"))

print(f"{total} intra-repo links checked across {len(FILES)} files")
for row in bad:
    print("  BROKEN %s: [%s](%s) -> %s" % row)
for f, d in dupes:
    print(f"  DUPLICATE slugs in {f}: {sorted(d)}")
if not bad and not dupes:
    print("OK - all links resolve, no duplicate heading slugs")
sys.exit(1 if (bad or dupes) else 0)
