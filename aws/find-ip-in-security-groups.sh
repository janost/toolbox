#!/bin/bash

# This script that queries all security groups in the current AWS region to find those
# containing rules with a specific IP address.
# The script checks if the IP address is contained in any CIDR range in the security group rules.
# This means it will find both exact matches and CIDR blocks that include the specified IP.

# Exit on error
set -e

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo "AWS CLI not found. Please install AWS CLI v2."
    exit 1
fi

# Check if IP address is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <ip-address>"
    echo "Example: $0 192.168.1.1"
    exit 1
fi

IP_ADDRESS="$1"


echo "Searching for security groups with rules containing IP address $IP_ADDRESS in region $AWS_REGION..."
echo "-----------------------------------"

# Get all security groups
SECURITY_GROUPS=$(aws ec2 describe-security-groups --query "SecurityGroups[*].[GroupId,GroupName]" --output text)

FOUND=0

while read -r GROUP_ID GROUP_NAME; do
    # Get security group details
    SG_DETAILS=$(aws ec2 describe-security-groups --group-ids "$GROUP_ID")
    
    # Check if IP address is in any ingress rule
    if echo "$SG_DETAILS" | grep -q "$IP_ADDRESS"; then
        echo "Found in Security Group: $GROUP_NAME ($GROUP_ID)"
        
        # Check ingress rules
        echo "$SG_DETAILS" | jq -r '.SecurityGroups[0].IpPermissions[] | select(.IpRanges[].CidrIp | contains("'"$IP_ADDRESS"'")) | 
            "  - Ingress: Protocol: \(.IpProtocol), Port Range: \(if .FromPort == .ToPort then .FromPort | tostring else "\(.FromPort)-\(.ToPort)" end), Source: \(.IpRanges[].CidrIp)"' 2>/dev/null || true
            
        # Check egress rules
        echo "$SG_DETAILS" | jq -r '.SecurityGroups[0].IpPermissionsEgress[] | select(.IpRanges[].CidrIp | contains("'"$IP_ADDRESS"'")) | 
            "  - Egress: Protocol: \(.IpProtocol), Port Range: \(if .FromPort == .ToPort then .FromPort | tostring else "\(.FromPort)-\(.ToPort)" end), Destination: \(.IpRanges[].CidrIp)"' 2>/dev/null || true
            
        echo ""
        FOUND=$((FOUND+1))
    fi
done <<< "$SECURITY_GROUPS"

if [ $FOUND -eq 0 ]; then
    echo "No security groups found containing rules with IP address $IP_ADDRESS"
else
    echo "Found $FOUND security group(s) containing rules with IP address $IP_ADDRESS"
fi
