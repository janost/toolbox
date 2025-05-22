#!/usr/bin/env bash

# CloudFormation Stack Drift Detection Script
# Detects drift in all CloudFormation stacks and lists drifted resources

# Define colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[0;33m'
readonly BLUE='\033[1;34m'
readonly BOLD='\033[1m'
readonly NC='\033[0m' # No Color

# Initialize log file with timestamp
readonly TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
readonly LOG_DIR="${HOME}/logs/cloudformation"
readonly LOG_FILE="${LOG_DIR}/drift-detection-${TIMESTAMP}.log"

# Define timeout settings
readonly MAX_WAIT_TIME=300  # 5 minutes timeout for drift detection
readonly SLEEP_TIME=2      # Time to sleep between status checks
readonly MAX_NULL_RETRIES=15  # Max retries for null drift status 
readonly NULL_RETRY_WAIT=4  # Seconds to wait between null status retries

# Create log directory if it doesn't exist
mkdir -p "${LOG_DIR}"

# Function to log messages
log() {
  local level="$1"
  local message="$2"
  local color=""
  
  case "${level}" in
    INFO) color="${BLUE}" ;;
    SUCCESS) color="${GREEN}" ;;
    WARNING) color="${YELLOW}" ;;
    ERROR) color="${RED}" ;;
    *) color="" ;;
  esac
  
  echo -e "${color}${level}: ${message}${NC}"
  echo "[$(date +"%Y-%m-%d %H:%M:%S")] ${level}: ${message}" >> "${LOG_FILE}"
}

# Get AWS region
region=${AWS_REGION:-$(aws configure get region)}
log "INFO" "=== CloudFormation Stack Drift Detection ==="
log "INFO" "Results will be saved to: ${LOG_FILE}"
log "INFO" "Using AWS region: ${region}"

# Check for required commands
for cmd in aws jq; do
  if ! command -v "${cmd}" &>/dev/null; then
    log "ERROR" "${cmd} is required but not installed."
    exit 1
  fi
done

# Get stacks directly into an array using AWS CLI JSON output and jq
log "INFO" "Fetching all active CloudFormation stacks..."

# Create a temporary JSON file to store stack data
tmp_stack_file=$(mktemp)

# Get all stack names as JSON
aws cloudformation list-stacks \
  --region "${region}" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE \
  UPDATE_ROLLBACK_COMPLETE IMPORT_COMPLETE IMPORT_ROLLBACK_COMPLETE \
  --output json > "${tmp_stack_file}"

# Extract stack names to an array using jq
mapfile -t stack_names < <(jq -r '.StackSummaries[].StackName' "${tmp_stack_file}")

stack_count=${#stack_names[@]}
if [[ ${stack_count} -eq 0 ]]; then
  log "WARNING" "No active CloudFormation stacks found."
  rm "${tmp_stack_file}"
  exit 0
fi

log "SUCCESS" "Found ${stack_count} active stacks."

# Initialize counters
drifted_count=0
in_sync_count=0
failed_count=0

# Process each stack
for stack_name in "${stack_names[@]}"; do
  echo -e "\n${BLUE}=== Processing stack: ${YELLOW}${stack_name}${BLUE} ===${NC}"
  
  # Initiate drift detection
  log "INFO" "Starting drift detection for stack: ${stack_name}"
  
  if ! drift_id=$(aws cloudformation detect-stack-drift \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query 'StackDriftDetectionId' \
    --output text 2>/dev/null); then
    log "ERROR" "Failed to initiate drift detection for stack: ${stack_name}"
    ((failed_count++))
    continue
  fi
  
  log "INFO" "Drift detection initiated with ID: ${drift_id}"
  
  # Wait for drift detection to complete
  status="IN_PROGRESS"
  echo -n "Waiting for drift detection to complete"
  
  elapsed_time=0
  
  while [[ "${status}" == "IN_PROGRESS" && ${elapsed_time} -lt ${MAX_WAIT_TIME} ]]; do
    echo -n "."
    sleep ${SLEEP_TIME}
    elapsed_time=$((elapsed_time + SLEEP_TIME))
    
    if ! detection_status=$(aws cloudformation describe-stack-drift-detection-status \
      --region "${region}" \
      --stack-drift-detection-id "${drift_id}" \
      --output json 2>/dev/null); then
      log "ERROR" "Failed to check drift detection status"
      ((failed_count++))
      continue 2  # Skip to next stack
    fi
    
    status=$(echo "${detection_status}" | jq -r '.DetectionStatus')
  done
  
  # Check if we timed out
  if [[ ${elapsed_time} -ge ${MAX_WAIT_TIME} ]]; then
    log "ERROR" "Drift detection timed out after ${MAX_WAIT_TIME} seconds"
    ((failed_count++))
    continue
  fi
  
  echo ""  # New line after progress dots
  
  # Get drift status
  drift_status=$(echo "${detection_status}" | jq -r '.StackDriftStatus')
  
  # Wait for drift status to become available (not null)
  null_retry_count=0
  
  while [[ "${drift_status}" == "null" && ${null_retry_count} -lt ${MAX_NULL_RETRIES} ]]; do
    null_retry_count=$((null_retry_count + 1))
    log "INFO" "Drift status is null, waiting for it to be computed (attempt ${null_retry_count}/${MAX_NULL_RETRIES})..."
    sleep ${NULL_RETRY_WAIT}
    
    # Fetch the detection status again
    if ! detection_status=$(aws cloudformation describe-stack-drift-detection-status \
      --region "${region}" \
      --stack-drift-detection-id "${drift_id}" \
      --output json 2>/dev/null); then
      log "ERROR" "Failed to check drift detection status during null retry"
      ((failed_count++))
      continue 2  # Skip to next stack
    fi
    
    drift_status=$(echo "${detection_status}" | jq -r '.StackDriftStatus')
  done
  
  # Check if we still have null drift status after all retries
  if [[ "${drift_status}" == "null" ]]; then
    log "WARNING" "Stack ${stack_name} drift status remained null after ${MAX_NULL_RETRIES} retries."
    log "WARNING" "This could indicate the stack contains resources that don't support drift detection."
    ((failed_count++))
    continue
  fi
  
  if [[ "${drift_status}" == "DRIFTED" ]]; then
    drifted_count=$((drifted_count+1))
    
    drifted_resource_count=$(echo "${detection_status}" | jq -r '.DriftedStackResourceCount')
    log "WARNING" "Stack ${stack_name} has drifted! (${drifted_resource_count} resources)"
    
    # Get drifted resources
    log "INFO" "Fetching drifted resources for stack: ${stack_name}"
    
    # Create a temporary file for the drift data
    tmp_drift_file=$(mktemp)
    
    # Use the proper AWS CLI command with correct parameters (MODIFIED and DELETED only)
    if ! aws cloudformation describe-stack-resource-drifts \
      --region "${region}" \
      --stack-name "${stack_name}" \
      --stack-resource-drift-status-filters MODIFIED DELETED \
      --output json > "${tmp_drift_file}" 2>/dev/null; then
      
      log "ERROR" "Failed to fetch drifted resources for stack: ${stack_name}"
      rm "${tmp_drift_file}"
    else
      # Process the drift data
      if jq -e '.StackResourceDrifts' "${tmp_drift_file}" > /dev/null; then
        resource_count=$(jq '.StackResourceDrifts | length' "${tmp_drift_file}")
        
        if [[ "${resource_count}" -gt 0 ]]; then
          log "WARNING" "Found ${resource_count} drifted resources in stack: ${stack_name}"
          
          # Log drifted resources
          echo -e "${BOLD}Drifted resources:${NC}"
          echo "Drifted resources for stack ${stack_name}:" >> "${LOG_FILE}"
          
          # Loop through drifted resources
          jq -r '.StackResourceDrifts[] | "  \(.LogicalResourceId) (\(.ResourceType)): \(.StackResourceDriftStatus)"' "${tmp_drift_file}" | 
          while IFS= read -r line; do
            if [[ -z "${line}" ]]; then
              continue
            fi
            
            # Extract drift status
            status=$(echo "${line}" | grep -oE '(MODIFIED|DELETED)$' || echo "UNKNOWN")
            
            # Color based on drift status
            case "${status}" in
              MODIFIED)
                echo -e "${YELLOW}${line}${NC}" 
                ;;
              DELETED)
                echo -e "${RED}${line}${NC}"
                ;;
              *)
                echo -e "${line}"
                ;;
            esac
            
            # Add to log file without color codes
            echo "${line}" >> "${LOG_FILE}"
          done
          
          # Show property differences for modified resources
          jq -c '.StackResourceDrifts[] | select(.StackResourceDriftStatus == "MODIFIED")' "${tmp_drift_file}" |
          while IFS= read -r resource; do
            if [[ -z "${resource}" ]]; then
              continue
            fi
            
            logical_id=$(echo "${resource}" | jq -r '.LogicalResourceId')
            
            echo -e "\n${BOLD}Property differences for ${logical_id}:${NC}"
            echo "Property differences for ${logical_id}:" >> "${LOG_FILE}"
            
            # Process property differences if they exist
            if echo "${resource}" | jq -e '.PropertyDifferences' > /dev/null; then
              echo "${resource}" | jq -r '.PropertyDifferences[] | 
                "  \(.PropertyPath): \(.ExpectedValue) -> \(.ActualValue) (\(.DifferenceType))"' |
              while IFS= read -r diff_line; do
                if [[ -z "${diff_line}" ]]; then
                  continue
                fi
                
                echo -e "    ${YELLOW}${diff_line}${NC}"
                echo "    ${diff_line}" >> "${LOG_FILE}"
              done
            else
              echo -e "    ${YELLOW}No detailed property differences available${NC}"
              echo "    No detailed property differences available" >> "${LOG_FILE}"
            fi
          done
        else
          log "INFO" "No MODIFIED or DELETED resources found in the drifted stack"
          log "INFO" "This may happen if resources were added or if unsupported resources were modified"
        fi
      else
        log "ERROR" "Invalid response format when fetching drifted resources"
      fi
      
      # Clean up temporary drift file
      rm "${tmp_drift_file}"
    fi
  elif [[ "${drift_status}" == "IN_SYNC" ]]; then
    in_sync_count=$((in_sync_count+1))
    log "SUCCESS" "Stack ${stack_name} is in sync with its template."
  else
    failed_count=$((failed_count+1))
    log "WARNING" "Stack ${stack_name} drift status: ${drift_status}"
  fi
  
  # Show progress
  echo -e "${GREEN}Progress: ${drifted_count}${NC}/${in_sync_count}/${failed_count} (drifted/in-sync/failed) of ${stack_count} total"
done

# Clean up temporary file
rm "${tmp_stack_file}"

# Display summary
echo -e "\n${BLUE}${BOLD}=== Drift Detection Summary ===${NC}"
log "INFO" "=== Drift Detection Summary ==="
log "INFO" "Total stacks processed: $((drifted_count + in_sync_count + failed_count))"
log "SUCCESS" "Stacks in sync: ${in_sync_count}"
log "WARNING" "Stacks drifted: ${drifted_count}"

if [[ ${failed_count} -gt 0 ]]; then
  log "ERROR" "Stacks with other status: ${failed_count}"
fi

log "INFO" "Detailed results saved to: ${LOG_FILE}"
echo -e "${GREEN}Detailed results saved to: ${LOG_FILE}${NC}"
