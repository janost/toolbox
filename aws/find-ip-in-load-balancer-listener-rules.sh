#!/bin/bash

# This is a script that queries all load balancers in the current AWS region to find those with listener rules containing a specific IP address.

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

echo "Searching for load balancers with listener rules containing IP address $IP_ADDRESS in region $AWS_REGION..."
echo "-----------------------------------"

# Get all load balancers (both ALB and NLB)
echo "Retrieving Application Load Balancers..."
APP_LBS=$(aws elbv2 describe-load-balancers --query "LoadBalancers[?Type=='application'].[LoadBalancerArn,LoadBalancerName]" --output text)

echo "Retrieving Network Load Balancers..."
NET_LBS=$(aws elbv2 describe-load-balancers --query "LoadBalancers[?Type=='network'].[LoadBalancerArn,LoadBalancerName]" --output text)

# Combine all load balancers
ALL_LBS="${APP_LBS}${NET_LBS}"

if [ -z "$ALL_LBS" ]; then
    echo "No load balancers found in region $AWS_REGION."
    exit 0
fi

FOUND=0

while read -r LB_ARN LB_NAME || [ -n "$LB_ARN" ]; do
    [ -z "$LB_ARN" ] && continue
    
    echo "Checking load balancer: $LB_NAME"
    
    # Get listeners for this load balancer
    LISTENERS=$(aws elbv2 describe-listeners --load-balancer-arn "$LB_ARN" --query "Listeners[*].ListenerArn" --output text)
    
    if [ -z "$LISTENERS" ]; then
        echo "  - No listeners found for this load balancer"
        continue
    fi
    
    LB_MATCH=0
    
    # Process each listener individually
    for LISTENER_ARN in $LISTENERS; do
        # Get rules for this listener
        RULES=$(aws elbv2 describe-rules --listener-arn "$LISTENER_ARN")
        
        # Check for source IP condition in rules
        if echo "$RULES" | jq -e '.Rules[].Conditions[] | select(.Field == "source-ip")' > /dev/null 2>&1; then
            # For each rule, check if it has the IP address
            MATCHING_RULES=$(echo "$RULES" | jq -r '.Rules[] | 
                select(.Conditions[] | select(.Field == "source-ip") | 
                    .SourceIpConfig.Values[] | 
                    contains("'"$IP_ADDRESS"'") or 
                    (split("/")[0] | split(".") as $ip | "'"$IP_ADDRESS"'" | split(".") as $search |
                    ($ip[0] == $search[0] and $ip[1] == $search[1] and $ip[2] == $search[2] and $ip[3] == $search[3]))
                ) | 
                {RuleArn, Priority, Actions: [.Actions[] | {Type, TargetGroupArn}]}')
            
            if [ -n "$MATCHING_RULES" ]; then
                if [ "$LB_MATCH" -eq 0 ]; then
                    echo "  ✓ Found rules matching IP $IP_ADDRESS"
                    LB_MATCH=1
                    FOUND=$((FOUND+1))
                fi
                
                # Process each matching rule
                echo "$MATCHING_RULES" | jq -c '.' | while read -r rule; do
                    PRIORITY=$(echo "$rule" | jq -r '.Priority')
                    ACTIONS=$(echo "$rule" | jq -r '.Actions | map(.Type) | join(", ")')
                    RULE_ARN=$(echo "$rule" | jq -r '.RuleArn')
                    
                    echo "    - Rule Priority: $PRIORITY"
                    echo "      Actions: $ACTIONS"
                    
                    # Get the specific source IP conditions that matched
                    SOURCE_IPS=$(echo "$RULES" | jq -r '.Rules[] | 
                        select(.RuleArn == "'"$RULE_ARN"'") | 
                        .Conditions[] | select(.Field == "source-ip") | 
                        .SourceIpConfig.Values[] | select(
                            contains("'"$IP_ADDRESS"'") or
                            (split("/")[0] | split(".") as $ip | "'"$IP_ADDRESS"'" | split(".") as $search |
                            ($ip[0] == $search[0] and $ip[1] == $search[1] and $ip[2] == $search[2] and $ip[3] == $search[3]))
                        )')
                    
                    echo "      Matching Source IPs: $SOURCE_IPS"
                done
            fi
        fi
    done
    
    if [ "$LB_MATCH" -eq 0 ]; then
        echo "  - No rules found with IP $IP_ADDRESS"
    fi
    
    echo ""
done <<< "$ALL_LBS"

if [ $FOUND -eq 0 ]; then
    echo "No load balancers found with listener rules containing IP address $IP_ADDRESS"
else
    echo "Found $FOUND load balancer(s) with listener rules containing IP address $IP_ADDRESS"
fi