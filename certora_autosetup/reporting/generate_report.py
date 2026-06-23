"""
UNUSED
Integration method for generating markdown reports from orchestrator.
"""

import subprocess
import sys
from pathlib import Path


def generate_sanity_report(job_url: str, output_dir: str = ".") -> None:
    """
    Generate a markdown report for sanity check results.
    
    Args:
        job_url: The Certora Prover job URL from sanity run
        output_dir: Directory to save the report (default: current directory)
    """
    print(f"📊 Generating sanity report from job: {job_url}")
    
    # Path to markdown reporter (now in same directory)
    markdown_reporter_path = Path(__file__).parent / "markdown_reporter.py"
    
    if not markdown_reporter_path.exists():
        print(f"❌ Markdown reporter not found at: {markdown_reporter_path}")
        return
    
    # Output file
    output_file = Path(output_dir) / "sanity_report.md"
    absolute_path = output_file.absolute()
    
    try:
        # Run the markdown reporter
        result = subprocess.run([
            sys.executable, str(markdown_reporter_path),
            job_url,
            "--output", str(absolute_path)
        ], capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0:
            print(f"✅ Sanity report generated: {output_file}")
            print(f"📄 Full path: {absolute_path}")
            print(f"📄 Review the report for any violated assertions")
            
            # Verify file exists
            if absolute_path.exists():
                print(f"✅ File verified at: {absolute_path}")
            else:
                print(f"⚠️  File not found at expected location: {absolute_path}")
        else:
            print(f"❌ Failed to generate report (exit code: {result.returncode}):")
            if result.stdout:
                print(f"   stdout: {result.stdout}")
            if result.stderr:
                print(f"   stderr: {result.stderr}")
            
            # Try to manually check if file was created anyway
            if absolute_path.exists():
                print(f"⚠️  File exists despite error: {absolute_path}")
            else:
                print(f"❌ No file created at: {absolute_path}")
            
    except subprocess.TimeoutExpired:
        print("❌ Report generation timed out")
    except Exception as e:
        print(f"❌ Error generating report: {e}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate sanity check markdown report")
    parser.add_argument("job_url", help="Certora Prover job URL")
    parser.add_argument("--output-dir", "-o", default=".", help="Output directory")
    
    args = parser.parse_args()
    
    generate_sanity_report(args.job_url, args.output_dir)