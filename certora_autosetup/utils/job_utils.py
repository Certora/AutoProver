"""Utilities for working with Certora job URLs, tarballs, and cloud data.

These are general-purpose job utilities used by both autosetup and preaudit.
"""

import hashlib
import re
import shutil
import tarfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from certora_autosetup.utils.constants import DIR_EXTRACTED_TARS
from certora_autosetup.utils.logger import logger


# Sidecar filename for tarball MD5 cache validation
_TARBALL_MD5_FILENAME = ".tarball_md5"

# Global lock dictionaries for preventing concurrent operations on the same job
_download_locks: dict[str, threading.Lock] = {}
_download_locks_lock = threading.Lock()
_extraction_locks: dict[str, threading.Lock] = {}
_extraction_locks_lock = threading.Lock()

# Default directories to exclude when extracting full tarballs
_DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = ("outputs",)
_DEFAULT_EXCLUDE_EXTENSIONS: tuple[str, ...] = (".html",)

COMPONENT = "JobUtils"

# Match Certora job URLs across all subdomains (prover, vaas-dev, vaas-stg, future vaas-*).
# Supports both /output/NUMBER/HASH (canonical) and /output/HASH (hash-only) shapes.
CERTORA_JOB_URL_RE = re.compile(
    r"https://[\w-]+\.certora\.com/output/(?:\d+/)?[A-Fa-f0-9]+"
)


def extract_job_url_from_text(output: str) -> Optional[str]:
    """Extract the last Certora job URL embedded in arbitrary text (e.g. certoraRun stdout)."""
    matches = CERTORA_JOB_URL_RE.findall(output)
    return matches[-1] if matches else None


def extract_job_id_from_url(url: str) -> str:
    """Extract job ID from Certora URL.

    Returns:
        - "{NUMBER}_{HASH}" for /output/NUMBER/HASH URLs (canonical)
        - "{HASH}"          for /output/HASH URLs (hash-only)
    Raises:
        ValueError if the URL has no /output/ segment or no segment after it.
    """
    parsed = urlparse(url)
    path_parts = parsed.path.split("/")
    try:
        output_idx = path_parts.index("output")
        first = path_parts[output_idx + 1]
    except (IndexError, ValueError):
        first = ""
    if not first:
        raise ValueError(
            f"Invalid Certora URL format: {url}. Expected: https://<host>.certora.com/output/[NUMBER/]HASH"
        )
    # If the segment after /output/ is all digits, treat it as NUMBER and grab HASH next.
    if first.isdigit() and len(path_parts) > output_idx + 2 and path_parts[output_idx + 2]:
        return f"{first}_{path_parts[output_idx + 2]}"
    return first


def extract_job_hash_from_url(url: str) -> str:
    """Return just the HASH portion of a Certora job URL, regardless of URL shape."""
    return job_id_to_hash(extract_job_id_from_url(url))


def job_id_to_hash(job_id: str) -> str:
    """Return the HASH portion of a job_id that may be either "NUMBER_HASH" or bare "HASH"."""
    return job_id.split("_", 1)[1] if "_" in job_id else job_id


def _compute_file_md5(file_path: Path) -> str:
    """Compute MD5 hash of a file."""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


def get_tarball_url(output_url: str) -> str:
    """Convert output URL to tarball URL."""
    return output_url


def download_tarball(job_id: str, output_url: str, cache_dir: Path) -> Optional[Path]:
    """Download a tarball from the cloud, with caching and thread safety.

    Uses ProverOutputAPI which handles authentication in both local (cookie-based)
    and CI (AWS credentials) environments.
    """
    from prover_output_utility import ProverOutputAPI

    with _download_locks_lock:
        if job_id not in _download_locks:
            _download_locks[job_id] = threading.Lock()
        job_lock = _download_locks[job_id]

    with job_lock:
        cached_path = cache_dir / f"{job_id}.tar.gz"

        if cached_path.exists():
            logger.log(f"Tarball for job {job_id} already cached", "INFO", COMPONENT)
            return cached_path

        logger.log(f"Downloading tarball for job {job_id}...", "INFO", COMPONENT)

        try:
            api = ProverOutputAPI()
            # fetch_outputs needs just the HASH; job_id may be "NUMBER_HASH" or bare "HASH"
            tar_bytes = api.data_fetcher.fetch_outputs(job_id_to_hash(job_id))
            cached_path.write_bytes(tar_bytes)
            logger.log(f"Downloaded tarball for job {job_id} ({len(tar_bytes)} bytes)", "INFO", COMPONENT)
            return cached_path
        except Exception as e:
            logger.log(f"Error downloading tarball for job {job_id}: {e}", "ERROR", COMPONENT)
            return None


def extract_from_tarball(
    tarball_path: Path,
    job_id: str,
    extraction_base: Path,
    subpath: str = "inputs/.certora_sources",
    exclude_dirs: tuple[str, ...] = _DEFAULT_EXCLUDE_DIRS,
    exclude_extensions: tuple[str, ...] = _DEFAULT_EXCLUDE_EXTENSIONS,
) -> Optional[Path]:
    """Extract a specific path from tarball to standard location, with caching and thread safety."""
    with _extraction_locks_lock:
        if job_id not in _extraction_locks:
            _extraction_locks[job_id] = threading.Lock()
        job_lock = _extraction_locks[job_id]

    with job_lock:
        extract_root = extraction_base / DIR_EXTRACTED_TARS / job_id
        extract_target = extract_root / subpath

        # Validate cache via MD5
        md5_file = extract_root / _TARBALL_MD5_FILENAME
        if extract_target.exists() and md5_file.exists():
            stored_md5 = md5_file.read_text().strip()
            current_md5 = _compute_file_md5(tarball_path)
            if stored_md5 == current_md5:
                logger.log(f"Using cached extraction for job {job_id}", "INFO", COMPONENT)
                return extract_target
            else:
                logger.log(f"Tarball changed for job {job_id}, re-extracting", "INFO", COMPONENT)

        logger.log(
            f"Extracting {'full tarball' if not subpath else subpath} for job {job_id}...",
            "INFO", COMPONENT,
        )

        try:
            if subpath:
                _extract_subpath(tarball_path, extract_root, extract_target, subpath)
            else:
                _extract_full(tarball_path, extract_root, exclude_dirs, exclude_extensions)

            # Write MD5 for cache validation
            try:
                md5_file = extract_root / _TARBALL_MD5_FILENAME
                md5_file.write_text(_compute_file_md5(tarball_path))
            except Exception:
                pass

            return extract_target
        except Exception as e:
            logger.log(f"Error extracting from tarball {tarball_path.name}: {e}", "ERROR", COMPONENT)
            return None


def _extract_subpath(tarball_path: Path, extract_root: Path, extract_target: Path, subpath: str) -> None:
    """Extract only a specific subpath from a tarball."""
    extract_root.mkdir(parents=True, exist_ok=True)
    temp_extract = extract_root.parent / f"{extract_root.name}_temp"
    temp_extract.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(tarball_path, "r:gz") as tar:
            members_to_extract = [m for m in tar.getmembers() if subpath in m.name]
            if not members_to_extract:
                logger.log(f"Subpath '{subpath}' not found in tarball {tarball_path.name}", "ERROR", COMPONENT)
                if temp_extract.exists():
                    shutil.rmtree(temp_extract)
                raise FileNotFoundError(f"Subpath '{subpath}' not found in tarball")
            tar.extractall(path=temp_extract, members=members_to_extract)

        # Find where the subpath ended up (may be nested under a top-level dir)
        actual = None
        direct_path = temp_extract / subpath
        if direct_path.exists():
            actual = direct_path
        else:
            for item in temp_extract.iterdir():
                if item.is_dir():
                    nested = item / subpath
                    if nested.exists():
                        actual = nested
                        break

        if not actual:
            shutil.rmtree(temp_extract)
            raise FileNotFoundError(f"Could not locate {subpath} in extracted content")

        if extract_target.exists():
            shutil.rmtree(extract_target)
        extract_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(actual), str(extract_target))
        shutil.rmtree(temp_extract)
        logger.log(f"Extracted {len(members_to_extract)} files from {subpath}", "INFO", COMPONENT)

    except Exception:
        if temp_extract.exists():
            shutil.rmtree(temp_extract)
        raise


def _extract_full(
    tarball_path: Path,
    extract_to: Path,
    exclude_dirs: tuple[str, ...],
    exclude_extensions: tuple[str, ...],
) -> None:
    """Extract full tarball, optionally excluding directories and extensions."""
    temp_extract = extract_to.parent / f"{extract_to.name}_temp"
    try:
        with tarfile.open(tarball_path, "r:gz") as tar:
            all_members = tar.getmembers()

            if exclude_dirs:
                top_level = {m.name.split("/", 1)[0] for m in all_members if m.name}
                wrapper = f"{next(iter(top_level))}/" if len(top_level) == 1 else ""

                def is_excluded(name: str) -> bool:
                    path = name[len(wrapper):] if wrapper else name
                    return any(path == d or path.startswith(f"{d}/") for d in exclude_dirs)

                all_members = [m for m in all_members if not is_excluded(m.name)]

            if exclude_extensions:
                all_members = [m for m in all_members if not m.name.endswith(exclude_extensions)]

            tar.extractall(path=temp_extract, members=all_members)

        extracted_items = list(temp_extract.iterdir())
        if len(extracted_items) == 1 and extracted_items[0].is_dir():
            nested_dir = extracted_items[0]
            if extract_to.exists():
                shutil.rmtree(extract_to)
            shutil.move(str(nested_dir), str(extract_to))
            shutil.rmtree(temp_extract)
        else:
            if extract_to.exists():
                shutil.rmtree(extract_to)
            shutil.move(str(temp_extract), str(extract_to))

    except Exception:
        if temp_extract.exists():
            shutil.rmtree(temp_extract)
        raise


def fetch_job_metadata(job_id: str, tmp_dir: Path | None = None, auth_cookies: Optional[Dict] = None) -> Dict:
    """Fetch job metadata from the Certora data API."""
    try:
        from prover_output_utility import ProverOutputAPI  # type: ignore[import-untyped]
        api = ProverOutputAPI()
        # api_base_url is resolved per-env (AISS_ENV / GITHUB_ENVIRONMENT) by POU, so dev/stg runs
        # hit data-api-dev / data-api-stg instead of always prod. Matches how get_failed_rules_from_api
        # / download_tarball in this module already rely on the env-aware ProverOutputAPI() instead of
        # hardcoding a host.
        endpoint = f"{api.api_base_url}/v1/domain/job-metadata/{job_id}"
        return api.fetch_custom_endpoint(endpoint)
    except Exception as e:
        logger.log(f"Error fetching metadata for job {job_id}: {e}", "WARNING", COMPONENT)
        return {}


def get_failed_rules_from_api(job_id: str) -> List[Tuple[str, Optional[List[str]]]]:
    """Get list of failed rules with their failed methods using ProverOutputAPI.

    Args:
        job_id: Job ID in format NUMBER_HASH (e.g., "60724_172e2b...")

    Returns:
        List of tuples: (rule_name, [method_names] or None)
    """
    from prover_output_utility import ProverOutputAPI  # type: ignore[import-untyped]

    hash_part = job_id_to_hash(job_id)
    api = ProverOutputAPI()

    try:
        check_results = api.get_violated_rules(hash_part)
    except Exception as e:
        raise RuntimeError(f"Failed to get violated rules from API: {e}")

    if not check_results:
        return []

    rules_dict: Dict[str, List[str]] = {}
    for result in check_results:
        rule_name = result.rule_name
        method_name = result.method_name
        if rule_name not in rules_dict:
            rules_dict[rule_name] = []
        if method_name and method_name not in rules_dict[rule_name]:
            rules_dict[rule_name].append(method_name)

    return [(name, methods if methods else None) for name, methods in rules_dict.items()]
