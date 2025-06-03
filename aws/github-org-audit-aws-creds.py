import os
import re
import sys
import logging
from datetime import datetime
from github import Github, UnknownObjectException, GithubException
from dotenv import load_dotenv

# Load environment variables from .env file (if it exists)
load_dotenv()

# --- Configuration ---
# Regex to heuristically detect AWS API interaction in workflow files.
# This includes common AWS CLI commands, official AWS actions, and AWS environment variable references.
AWS_KEYWORD_REGEX = re.compile(
    r'aws-actions/configure-aws-credentials|'
    r'aws-actions/aws-sam-cli-action|'
    r'aws\s+cli|aws\s+sts|aws\s+ecr|aws\s+s3|aws\s+lambda|aws\s+ecs|aws\s+eks|'
    r'AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|'
    r'terraform|cdk',
    re.IGNORECASE
)

# Regex to detect common AWS credential names in variables/secrets (case-insensitive).
# This regex is used to *extract* the names to display in the report.
AWS_CREDENTIAL_NAMES_SEARCH_PATTERN = r'AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN'
AWS_CREDENTIAL_NAMES_REGEX = re.compile(
    f'({AWS_CREDENTIAL_NAMES_SEARCH_PATTERN})',
    re.IGNORECASE
)

# --- Logging Setup ---
# Set up a logger that prints INFO and ERROR messages to stderr (console)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(logging.Formatter('INFO: %(message)s'))
logger.addHandler(console_handler)

def log_error(message: str):
    """Logs an error message to stderr."""
    logger.error(f'ERROR: {message}')

def log_info(message: str):
    """Logs an informational message to stderr."""
    logger.info(message)

# --- GitHub Auditor Class ---
class GitHubAuditor:
    def __init__(self, github_token: str):
        """Initializes the GitHub Auditor with a GitHub token."""
        if not github_token:
            log_error("GitHub token not found. Please set GITHUB_TOKEN environment variable.")
            sys.exit(1)
        self.g = Github(github_token)

    def get_organization_repos(self, org_name: str):
        """Fetches all non-archived repositories for a given GitHub organization."""
        try:
            log_info(f"Querying organization '{org_name}'...")
            org = self.g.get_organization(org_name)
            log_info(f"Fetching repositories for '{org_name}' (this might take a while for large organizations)...")
            # Filter out archived repositories during iteration
            repos = [repo for repo in org.get_repos() if not repo.archived]
            log_info(f"Found {len(repos)} non-archived repositories.")
            return repos
        except UnknownObjectException:
            log_error(f"Organization '{org_name}' not found or you don't have access.")
            sys.exit(1)
        except GithubException as e:
            log_error(f"GitHub API error fetching repositories for '{org_name}': {e.status} {e.data.get('message', 'No message')}")
            sys.exit(1)
        except Exception as e:
            log_error(f"An unexpected error occurred while fetching organization repositories: {e}")
            sys.exit(1)

    def analyze_workflow_content(self, repo, workflow_path: str) -> str:
        """
        Analyzes a single workflow file for AWS API related keywords.
        Returns "YES", "NO", or an error message.
        """
        try:
            contents = repo.get_contents(workflow_path)
            if contents and hasattr(contents, 'decoded_content'):
                content = contents.decoded_content.decode('utf-8')
                if AWS_KEYWORD_REGEX.search(content):
                    return "YES"
                return "NO"
            return "No content found"
        except UnknownObjectException:
            return "File not found or accessible"
        except GithubException as e:
            return f"API error fetching content: {e.status} {e.data.get('message', 'No message')}"
        except Exception as e:
            return f"Error analyzing content: {type(e).__name__}: {e}"

    def check_workflows(self, repo) -> tuple[str, list[dict]]:
        """
        Checks a repository for GitHub Actions workflows and analyzes them for AWS API interaction.
        Returns a tuple: (status_string, list_of_workflow_details).
        status_string examples: "YES", "NO", "NO (empty directory)", "UNKNOWN (API error)".
        workflow_details is a list of dicts: [{"name": "ci.yml", "path": ".github/workflows/ci.yml", "aws_api_related": "YES"}]
        """
        workflow_details = []
        try:
            # PyGithub's get_contents() for a directory returns a list of ContentFile objects.
            workflow_dir_contents = repo.get_contents(".github/workflows")
            
            if not workflow_dir_contents:
                return "NO (empty directory)", [] # Directory exists but is empty

            if not isinstance(workflow_dir_contents, list):
                # This could happen if .github/workflows points to a file, not a directory.
                return "NO (path is a file, not directory)", []

            workflows_found = False
            for content_file in workflow_dir_contents:
                if content_file.type == "file":
                    workflows_found = True
                    aws_api_status = self.analyze_workflow_content(repo, content_file.path)
                    workflow_details.append({
                        "name": content_file.name,
                        "path": content_file.path,
                        "aws_api_related": aws_api_status
                    })
            if not workflows_found:
                 return "NO (no workflow files found in directory)", [] # Directory exists but contains no files
            return "YES", workflow_details

        except UnknownObjectException:
            # This means the .github/workflows path does not exist in the repository.
            return "NO", []
        except GithubException as e:
            log_error(f"  Failed to query workflows for '{repo.full_name}'. API error: {e.status} {e.data.get('message', 'No message')}")
            return "UNKNOWN (API error)", []
        except Exception as e:
            log_error(f"  An unexpected error occurred while checking workflows for '{repo.full_name}': {type(e).__name__}: {e}")
            return "UNKNOWN (unexpected error)", []

    def check_aws_credentials_in_vars_and_secrets(self, repo) -> tuple[str, list[str]]:
        """
        Checks for static AWS credentials defined as repository variables or secrets.
        Returns a tuple: (status_string, list_of_credential_names_found).
        status_string: "YES" or "NO".
        list_of_credential_names_found: e.g., ["Variable: AWS_ACCESS_KEY_ID", "Secret: AWS_SECRET_ACCESS_KEY"].
        """
        found_creds_names = []
        api_errors_occurred = False

        # Check repository variables
        try:
            variables = repo.get_variables()
            for var in variables:
                if AWS_CREDENTIAL_NAMES_REGEX.search(var.name):
                    found_creds_names.append(f"Variable: {var.name}")
        except GithubException as e:
            log_error(f"    Failed to list variables for '{repo.full_name}': {e.status} {e.data.get('message', 'No message')}")
            found_creds_names.append("Variables (API Error)")
            api_errors_occurred = True
        except Exception as e:
            log_error(f"    An unexpected error occurred checking variables for '{repo.full_name}': {type(e).__name__}: {e}")
            found_creds_names.append("Variables (Unexpected Error)")
            api_errors_occurred = True

        # Check repository secrets
        try:
            secrets = repo.get_secrets()
            for secret in secrets:
                if AWS_CREDENTIAL_NAMES_REGEX.search(secret.name):
                    found_creds_names.append(f"Secret: {secret.name}")
        except GithubException as e:
            log_error(f"    Failed to list secrets for '{repo.full_name}': {e.status} {e.data.get('message', 'No message')}")
            found_creds_names.append("Secrets (API Error)")
            api_errors_occurred = True
        except Exception as e:
            log_error(f"    An unexpected error occurred checking secrets for '{repo.full_name}': {type(e).__name__}: {e}")
            found_creds_names.append("Secrets (Unexpected Error)")
            api_errors_occurred = True

        status = "YES" if found_creds_names and not api_errors_occurred else "NO"
        return status, found_creds_names

# --- Main Script Logic ---
def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <github-organization>")
        print("\nThis script audits GitHub repositories in a given organization for GitHub Actions workflows,")
        print("AWS API interactions in workflows, and static AWS credentials in variables/secrets.")
        print("\nIt outputs a concise, formatted report to a timestamped file,")
        print("listing ONLY repositories where static AWS credentials were found.")
        print("\nRequirements:")
        print("- PyGithub, python-dotenv installed (`pip install PyGithub python-dotenv`).")
        print("- GitHub Personal Access Token (PAT) set as GITHUB_TOKEN environment variable.")
        print("  PAT needs 'repo' scope for private repositories, variables, and secrets access.")
        sys.exit(1)

    org_name = sys.argv[1]
    github_token = os.getenv("GITHUB_TOKEN")

    auditor = GitHubAuditor(github_token)
    repos = auditor.get_organization_repos(org_name)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"github_audit_report_{org_name}_{timestamp}.txt"

    reports_to_print = [] # Stores formatted report blocks for repositories with findings.

    total_repos = len(repos)
    for i, repo in enumerate(repos):
        log_info(f"\n--- Processing Repository ({i+1}/{total_repos}): {repo.full_name} ---")

        # Collect Workflow Information
        log_info("  Collecting workflow details...")
        workflows_status, workflow_details = auditor.check_workflows(repo)

        # Collect AWS Credential Information
        log_info("  Collecting AWS credentials info...")
        aws_creds_status, aws_creds_names = auditor.check_aws_credentials_in_vars_and_secrets(repo)

        # If static AWS credentials are found, prepare report block for this repository
        if aws_creds_status == "YES":
            log_info(f"  Static AWS credentials found for {repo.full_name}. Adding to report.")
            
            repo_report_block = f"Repository: {repo.full_name}\n"
            repo_report_block += f"  Static AWS Credentials Found: {aws_creds_status}\n"
            repo_report_block += f"    Names checked (case-insensitive): {AWS_CREDENTIAL_NAMES_SEARCH_PATTERN.replace('|', ', ')}\n"
            repo_report_block += f"    Names found: {', '.join(aws_creds_names) if aws_creds_names else 'None found'}\n"
            repo_report_block += "    Note: Only names are checked. Actual value verification is not possible for security reasons.\n"
            
            repo_report_block += f"  Workflows found: {workflows_status}"
            if workflows_status == "YES" and workflow_details:
                repo_report_block += "\n  Workflow files:"
                for wf in workflow_details:
                    repo_report_block += f"\n    - {wf['name']} (Path: {wf['path']}): AWS API related: {wf['aws_api_related']}"
            elif workflows_status.startswith("NO ("): # For specific "NO" reasons like empty directory or file not dir
                 repo_report_block += f" ({workflows_status.replace('NO (','').replace(')','')})"
            
            repo_report_block += "\n\n" # Two newlines for readability between repo blocks.
            reports_to_print.append(repo_report_block)
        else:
            log_info(f"  No static AWS credentials found for {repo.full_name}.")

    # Write the final report to the designated output file
    try:
        with open(output_filename, 'w', encoding='utf-8') as f:
            f.write("GitHub Repository Audit Report\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Organization: {org_name}\n")
            f.write("\n")
            f.write("--- Repositories with Static AWS Credentials Detected ---\n")
            f.write("\n")

            if not reports_to_print:
                f.write(f"No repositories found with static AWS credentials in organization '{org_name}'.\n")
            else:
                for report_block in reports_to_print:
                    f.write(report_block)
            f.write("--- End of Report ---\n")
        log_info(f"\nAnalysis complete for organization: {org_name}")
        log_info(f"Report saved to: {output_filename}")
    except IOError as e:
        log_error(f"Failed to write report to file '{output_filename}': {e}")
        sys.exit(1)
    except Exception as e:
        log_error(f"An unexpected error occurred while writing the report: {type(e).__name__}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
