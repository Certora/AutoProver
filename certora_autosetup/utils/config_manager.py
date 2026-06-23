# A bare-bones version of Jaroslav's config manager


def convert_solc_version_to_certora_format(version: str) -> str:
    """
    Convert solc version from standard format to Certora format.

    Args:
        version: Version in standard format (e.g., "0.8.19")

    Returns:
        Version in Certora format (e.g., "solc8.19")
    """
    # Remove any leading 'v' if present
    version_str = version.lstrip("v")

    # Convert "0.8.19" to "solc8.19"
    if version_str.startswith("0."):
        # Remove the '0.' prefix and add 'solc' prefix
        return f"solc{version_str[2:]}"
    else:
        # If it doesn't start with '0.', just add 'solc' prefix
        return f"solc{version_str}"


def certora_format_to_raw_version(certora_version: str) -> str | None:
    """Inverse of convert_solc_version_to_certora_format.

    "solc8.19" -> "0.8.19", "solc-0.8.19" -> "0.8.19", "0.8.19" -> "0.8.19".
    Returns None if the input doesn't look like any known solc version form.
    """
    if not certora_version:
        return None

    s = certora_version.strip()
    if s.startswith("solc-"):
        s = s[len("solc-"):]
    elif s.startswith("solc"):
        s = s[len("solc"):]
        if s and not s.startswith("0."):
            s = f"0.{s}"

    if s.count(".") >= 1 and s[0].isdigit():
        return s
    return None