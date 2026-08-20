from pathlib import Path

def src_path_to_module(src_file: str) -> str:
    """
    Convert a file path under src/ to a Rust module path relative to `crate`.
 
    Examples
        src/contract.rs          -> crate::contract
        src/foo/bar.rs           -> crate::foo::bar
        src/foo/mod.rs           -> crate::foo
        src/lib.rs               -> crate            (the crate root itself)
    """
    rel = Path(src_file)
 
    parts = list(rel.parts)
    parts = parts[1:]
 
    # Drop the file extension from the last component
    last = parts[-1]
    stem = Path(last).stem          # e.g. "contract" from "contract.rs"
 
    if stem in ("lib", "main"):
        # The crate root – no extra segment
        parts = parts[:-1]
    elif stem == "mod":
        # mod.rs represents the parent module
        parts = parts[:-1]
    else:
        parts = parts[:-1] + [stem]
 
    if not parts:
        return "crate"
    return "crate::" + "::".join(parts)


