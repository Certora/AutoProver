#!/usr/bin/env python3
"""
certora_wget.py

Simple authenticated downloader for Certora prover outputs using certora-login.

Uses certora-login for authentication - no browser cookie extraction needed.

Dependencies:
  - Required: certora-login (from certora-cloud-cli repo)
      pip install "certora-login @ git+ssh://git@github.com/Certora/certora-cloud-cli.git#subdirectory=packages/certora_login"
  - Optional: requests (for Python download fallback)
      pip install requests

Run:
  python3 certora_wget.py "https://prover.certora.com/some/path" out.html

Security:
  - Uses certora-login's secure credential storage
  - Automatically refreshes tokens on 401/403 errors
"""
import os, sys, shutil, subprocess, argparse
from typing import Optional, Dict

# Suppress keyring warnings before importing certora_login
try:
    from certora_autosetup.utils.logger import logger as _preaudit_logger  # noqa: F401
except ImportError:
    pass  # Running standalone, warnings will show

# Try to import certora_login
try:
    from certora_login import login, delete_credentials  # type: ignore[import-untyped]
    HAVE_CERTORA_LOGIN = True
except ImportError:
    login = None  # type: ignore[assignment]
    delete_credentials = None  # type: ignore[assignment]
    HAVE_CERTORA_LOGIN = False

# ---------- Utilities ----------

class AuthenticationError(Exception):
    """Raised when authentication fails or credentials are missing."""
    pass

class DownloadError(Exception):
    """Raised when download fails."""
    pass

class _Logger:
    """Logger using the standard certora_autosetup logger."""

    def log(self, message: str, level: str = "INFO"):
        """Log a message."""
        try:
            from certora_autosetup.utils.logger import logger as _std_logger
            _std_logger.log(message, level, "CertoraWget")
            return
        except ImportError:
            pass
        # Console fallback
        if level == "ERROR":
            print(f"ERROR: {message}", file=sys.stderr)
        elif level == "WARNING":
            print(f"⚠️  {message}", file=sys.stderr)
        else:
            print(message, file=sys.stderr)

_logger = _Logger()

# ---------- Authentication using certora-login ----------

def get_auth_cookies() -> Dict[str, str]:
    """
    Get authentication cookies using certora-login.

    Returns:
        Dictionary of cookie name -> value

    Raises:
        AuthenticationError: If certora-login is not available or login fails
    """
    if os.getenv("CI"):
        return {}

    if not HAVE_CERTORA_LOGIN:
        msg = "certora-login package is required.\nInstall with: pip install \"certora-login @ git+ssh://git@github.com/Certora/certora-cloud-cli.git#subdirectory=packages/certora_login\""
        _logger.log(msg, "ERROR")
        raise AuthenticationError(msg)

    try:
        _logger.log("🔐 Authenticating with Certora...", "DEBUG")
        assert login is not None, "login function is not available"
        credentials = login(env="prod", force_file=True)

        # Convert credentials to cookie format
        cookies = {
            "user": credentials.get("user"),
            "certoraToken": credentials.get("certoraToken"),
            "certoraRefreshToken": credentials.get("certoraRefreshToken"),
        }

        # Verify all needed cookies are present
        needed = ("user", "certoraToken", "certoraRefreshToken")
        if not all(cookies.get(k) for k in needed):
            missing = [k for k in needed if not cookies.get(k)]
            msg = f"Authentication incomplete, missing: {missing}"
            _logger.log(msg, "ERROR")
            raise AuthenticationError(msg)

        _logger.log("✅ Authentication successful", "DEBUG")
        return cookies

    except AuthenticationError:
        raise
    except Exception as e:
        msg = f"Authentication failed: {e}"
        _logger.log(msg, "ERROR")
        raise AuthenticationError(msg) from e

def refresh_auth_tokens() -> Optional[Dict[str, str]]:
    """
    Refresh authentication tokens using certora-login.

    This deletes old credentials and forces a fresh login.

    Returns:
        New cookie map with refreshed tokens, or None if refresh failed
    """
    if not HAVE_CERTORA_LOGIN:
        _logger.log("Cannot refresh tokens: certora_login not installed", "ERROR")
        _logger.log("Install with: pip install \"certora-login @ git+ssh://git@github.com/Certora/certora-cloud-cli.git#subdirectory=packages/certora_login\"", "INFO")
        return None

    try:
        _logger.log("🔄 Deleting expired credentials...", "INFO")
        assert delete_credentials is not None, "delete_credentials function is not available"
        delete_credentials()

        _logger.log("🔐 Logging in to get fresh tokens...", "INFO")
        assert login is not None, "login function is not available"
        credentials = login(env="prod", force_file=True)

        # Convert credentials to cookie format
        new_cookies = {
            "user": credentials.get("user"),
            "certoraToken": credentials.get("certoraToken"),
            "certoraRefreshToken": credentials.get("certoraRefreshToken"),
        }

        # Verify all needed cookies are present
        needed = ("user", "certoraToken", "certoraRefreshToken")
        if all(new_cookies.get(k) for k in needed):
            _logger.log("✅ Successfully refreshed authentication tokens", "INFO")
            return new_cookies
        else:
            missing = [k for k in needed if not new_cookies.get(k)]
            _logger.log(f"Refreshed credentials incomplete, missing: {missing}", "ERROR")
            return None

    except Exception as e:
        _logger.log(f"Failed to refresh tokens: {e}", "ERROR")
        return None


# ---------- Download helper ----------

def build_cookie_header(cookie_map: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie_map.items())

def run_wget_or_curl(url: str, cookie_map: Dict[str, str], outpath: str, retry_on_auth_error: bool = True) -> int:
    """
    Download URL using cookies, with automatic token refresh on 401/403 errors.

    Args:
        url: URL to download
        cookie_map: Dictionary of cookie name -> value
        outpath: Output file path
        retry_on_auth_error: If True, will refresh tokens and retry once on 401/403 errors

    Returns:
        Exit code (0 = success)
    """
    cookie_header = build_cookie_header(cookie_map)
    wget = shutil.which("wget")
    curl = shutil.which("curl")

    # Helper to check if response is an authentication error (401 or 403)
    def is_auth_error(returncode: int, tool: str) -> bool:
        # wget returns 8 for server errors (including 401/403)
        # curl returns 22 for HTTP errors >= 400 (including 401/403)
        if tool == "wget" and returncode == 8:
            return True
        if tool == "curl" and returncode == 22:
            return True
        return False

    # Try with wget
    if wget:
        cmd = [wget, "--header", f"Cookie: {cookie_header}", "-O", outpath, url]
        rc = subprocess.run(cmd, capture_output=True).returncode

        if rc == 0:
            return 0
        elif is_auth_error(rc, "wget") and retry_on_auth_error:
            _logger.log("Got authentication error (401/403), refreshing tokens...", "WARNING")
            refreshed_cookies = refresh_auth_tokens()
            if refreshed_cookies:
                # Retry with refreshed tokens
                cookie_header = build_cookie_header(refreshed_cookies)
                cmd = [wget, "--header", f"Cookie: {cookie_header}", "-O", outpath, url]
                return subprocess.run(cmd).returncode
        return rc

    # Try with curl
    if curl:
        cmd = [curl, "-L", "-sS", "-H", f"Cookie: {cookie_header}", "-o", outpath, url]
        rc = subprocess.run(cmd, capture_output=True).returncode

        if rc == 0:
            return 0
        elif is_auth_error(rc, "curl") and retry_on_auth_error:
            _logger.log("Got authentication error (401/403), refreshing tokens...", "WARNING")
            refreshed_cookies = refresh_auth_tokens()
            if refreshed_cookies:
                # Retry with refreshed tokens
                cookie_header = build_cookie_header(refreshed_cookies)
                cmd = [curl, "-L", "-sS", "-H", f"Cookie: {cookie_header}", "-o", outpath, url]
                return subprocess.run(cmd).returncode
        return rc

    # Fallback to requests
    try:
        import requests  # type: ignore[import-untyped]
    except Exception:
        msg = "Neither wget nor curl found and 'requests' not installed. Install one of them."
        _logger.log(msg, "ERROR")
        raise DownloadError(msg)

    with requests.Session() as s:
        # Set cookies explicitly
        # Use .certora.com (with leading dot) to work across all Certora subdomains
        for k, v in cookie_map.items():
            s.cookies.set(k, v, domain=".certora.com", path="/")

        try:
            r = s.get(url, allow_redirects=True, timeout=60)
            r.raise_for_status()
            with open(outpath, "wb") as f:
                f.write(r.content)
            return 0
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403) and retry_on_auth_error:
                _logger.log(f"Got {e.response.status_code} error, refreshing tokens...", "WARNING")
                refreshed_cookies = refresh_auth_tokens()
                if refreshed_cookies:
                    # Retry with refreshed tokens
                    s.cookies.clear()
                    for k, v in refreshed_cookies.items():
                        s.cookies.set(k, v, domain=".certora.com", path="/")
                    r = s.get(url, allow_redirects=True, timeout=60)
                    r.raise_for_status()
                    with open(outpath, "wb") as f:
                        f.write(r.content)
                    return 0
            raise

    return 0

# ---------- Utility Function for Direct Invocation ----------

def download_with_auth(url: str, output_path: str, convert_to_zip_output: bool = False, auth_cookies: Optional[Dict[str, str]] = None) -> int:
    """
    Download a file from Certora with authentication (utility function for programmatic use).

    This function wraps the main download logic without using argparse,
    suitable for calling from other Python modules.

    Args:
        url: URL to download (should be on https://prover.certora.com/...)
        output_path: Output file path (str or Path)
        convert_to_zip_output: If True, convert /output to /zipOutput in URL
        auth_cookies: Optional pre-fetched auth cookies to reuse (avoids concurrent auth calls)

    Returns:
        int: Exit code (0 = success, non-zero = failure)

    Example:
        >>> from certora_autosetup.utils.certora_wget import download_with_auth
        >>> exit_code = download_with_auth(
        ...     "https://prover.certora.com/output/12345/abc",
        ...     "/tmp/output.json"
        ... )
        >>> if exit_code == 0:
        ...     print("Success!")
    """
    try:
        # Get authentication cookies (or use provided ones)
        if auth_cookies is None:
            cookie_map = get_auth_cookies()
        else:
            cookie_map = auth_cookies

        # Optionally convert /output to /zipOutput
        actual_url = url
        if convert_to_zip_output:
            actual_url = url.replace("/output", "/zipOutput")

        # Run downloader
        return run_wget_or_curl(actual_url, cookie_map, str(output_path))

    except (AuthenticationError, DownloadError) as e:
        # Expected errors (already logged)
        return 1
    except Exception as e:
        # Unexpected errors
        _logger.log(f"Download failed: {e}", "ERROR")
        return 1

# ---------- Main ----------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fetch authenticated Certora content using certora-login.")
    ap.add_argument("url", help="URL to fetch (should be on https://prover.certora.com/...)")
    ap.add_argument("output", nargs="?", default="output.html", help="Output file (default output.html)")
    ap.add_argument("--zip-output", action="store_true", help="Convert /output to /zipOutput in URL")
    args = ap.parse_args()

    try:
        # Get authentication cookies
        cookie_map = get_auth_cookies()

        # optionally convert /output to /zipOutput
        url = args.url
        if args.zip_output:
            url = url.replace("/output", "/zipOutput")
            _logger.log(f"Converted URL to use /zipOutput: {url}", "INFO")

        # run downloader
        out = args.output
        _logger.log(f"Downloading {url} -> {out} ...", "INFO")
        rc = run_wget_or_curl(url, cookie_map, out)

        if rc == 0:
            _logger.log("Download succeeded.", "INFO")
            sys.exit(0)
        else:
            _logger.log(f"Downloader returned exit code {rc}", "ERROR")
            sys.exit(rc)

    except (AuthenticationError, DownloadError):
        # Expected errors (already logged)
        sys.exit(1)
    except Exception as e:
        _logger.log(f"Download failed: {e}", "ERROR")
        sys.exit(1)
