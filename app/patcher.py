"""Apply the DEFAULT_REGION_CFG patch to MyMesh.cpp."""
import os

MARKER = "// BEGIN_DEFAULT_REGION_CFG_PATCH"

PATCH_FUNCTION = """\

// BEGIN_DEFAULT_REGION_CFG_PATCH
#ifdef DEFAULT_REGION_CFG
static void applyDefaultRegionCfg(RegionMap& map) {
  // Wildcard is intentionally left at flags=0 (allow flood) — MeshCore default.
  // This function only adds named regions.
  char cfg[] = DEFAULT_REGION_CFG;
  char* entry = strtok(cfg, ";");
  while (entry) {
    char* colon = strrchr(entry, ':');
    bool allow = colon && colon[1] == 'A';
    if (colon) *colon = '\\0';
    char* slash = strchr(entry, '/');
    uint16_t parent_id = 0;
    if (slash) {
      *slash = '\\0';
      auto parent = map.findByName(slash + 1);
      if (parent) parent_id = parent->id;
    }
    auto r = map.putRegion(entry, parent_id);
    if (r) r->flags = allow ? 0 : REGION_DENY_FLOOD;
    entry = strtok(NULL, ";");
  }
}
#endif
// END_DEFAULT_REGION_CFG_PATCH
"""

PATCH_CALL = """\

#ifdef DEFAULT_REGION_CFG
  if (region_map.getCount() == 0) {
    applyDefaultRegionCfg(region_map);
    region_map.save(_fs);
  }
#endif\
"""

LOAD_LINE = "  region_map.load(_fs);"
BEGIN_FUNC = "void MyMesh::begin(FILESYSTEM *fs) {"


def apply(meshcore_path: str | None = None) -> None:
    path = meshcore_path or os.environ.get("MESHCORE_PATH", "/meshcore")
    target = os.path.join(path, "examples/simple_repeater/MyMesh.cpp")

    with open(target) as f:
        content = f.read()

    if MARKER in content:
        print(f"patcher: already applied, skipping {target}")
        return

    if BEGIN_FUNC not in content:
        raise RuntimeError(f"patcher: could not find '{BEGIN_FUNC}' in {target}")

    if LOAD_LINE not in content:
        raise RuntimeError(f"patcher: could not find '{LOAD_LINE}' in {target}")

    content = content.replace(BEGIN_FUNC, PATCH_FUNCTION + BEGIN_FUNC, 1)
    content = content.replace(LOAD_LINE, LOAD_LINE + PATCH_CALL, 1)

    with open(target, "w") as f:
        f.write(content)

    print(f"patcher: patch applied to {target}")


if __name__ == "__main__":
    apply()
