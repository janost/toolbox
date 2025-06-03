#!/usr/bin/env python3
"""
ECS Shell - A utility to open an interactive shell to an AWS ECS task.

Usage:
    ./ecs-shell.py [CLUSTER_NAME] [SERVICE_NAME] [-t TASK_ID] [-c CONTAINER_NAME] [-s SHELL_OR_COMMAND]
"""

import argparse
import datetime
import subprocess
import sys
from typing import Dict, List, Optional, Any, TypedDict, NotRequired, cast

import boto3
from botocore.exceptions import ClientError

# Type definitions for ECS resources
class Container(TypedDict):
    """Type definition for container dictionaries."""
    name: str
    containerArn: str
    lastStatus: str
    image: str
    cpu: NotRequired[str]
    memory: NotRequired[str]


class ContainerDefinition(TypedDict):
    """Type definition for container definition dictionaries."""
    name: str
    image: str
    essential: bool
    environment: NotRequired[List[Dict[str, str]]]


class Task(TypedDict):
    """Type definition for task dictionaries."""
    taskArn: str
    taskDefinitionArn: str
    clusterArn: str
    lastStatus: str
    startedAt: NotRequired[datetime.datetime]  # boto3 returns this as a datetime
    containers: List[Container]


class TaskDefinition(TypedDict):
    """Type definition for task definition dictionaries."""
    taskDefinitionArn: str
    containerDefinitions: List[ContainerDefinition]
    family: str
    revision: int


class Service(TypedDict):
    """Type definition for service dictionaries."""
    serviceName: str
    serviceArn: str
    taskDefinition: str
    desiredCount: int
    runningCount: int


# Constants
DEFAULT_SHELL = "bash"


def parse_args() -> argparse.Namespace:
    """Parse and validate command line arguments."""
    parser = argparse.ArgumentParser(
        description="Open an interactive shell to an AWS ECS task.",
    )
    parser.add_argument(
        "cluster_name",
        metavar="CLUSTER_NAME",
        nargs="?",
        help="Name of the ECS cluster",
    )
    parser.add_argument(
        "service_name",
        metavar="SERVICE_NAME",
        nargs="?",
        help="Name of the ECS service",
    )
    parser.add_argument(
        "-t",
        "--task",
        help="Task ID to connect to (defaults to most recently started task)",
    )
    parser.add_argument(
        "-c",
        "--container",
        help="Container name to connect to (determined automatically if possible)",
    )
    parser.add_argument(
        "-s",
        "--shell",
        default=DEFAULT_SHELL,
        help="Shell or command to execute (defaults to bash)",
    )
    return parser.parse_args()


def get_all_clusters() -> List[str]:
    """Get a list of all ECS clusters."""
    ecs = boto3.client("ecs")
    cluster_arns = []
    
    paginator = ecs.get_paginator("list_clusters")
    for page in paginator.paginate():
        cluster_arns.extend(page.get("clusterArns", []))
    
    return [cluster_arn.split("/")[-1] for cluster_arn in cluster_arns]


def get_services_for_cluster(cluster: str) -> List[str]:
    """Get all services for a given cluster."""
    ecs = boto3.client("ecs")
    service_arns = []
    
    paginator = ecs.get_paginator("list_services")
    for page in paginator.paginate(cluster=cluster):
        service_arns.extend(page.get("serviceArns", []))
    
    return [service_arn.split("/")[-1] for service_arn in service_arns]


def get_service_details(cluster: str, service: str) -> Service:
    """Get details about a service."""
    ecs = boto3.client("ecs")
    
    response = ecs.describe_services(
        cluster=cluster,
        services=[service]
    )
    
    if not (services := response.get("services")):
        raise ValueError(f"Service '{service}' not found in cluster '{cluster}'")
    
    return cast(Service, services[0])


def get_task_definition(task_definition_arn: str) -> TaskDefinition:
    """Get details about a task definition."""
    ecs = boto3.client("ecs")
    
    task_def = task_definition_arn.split("/")[-1]
    if ":" in task_def:
        task_def = task_def.split(":")[0]  # Remove revision if present
    
    response = ecs.describe_task_definition(
        taskDefinition=task_def
    )
    
    return cast(TaskDefinition, response["taskDefinition"])


def get_tasks_for_service(cluster: str, service: str) -> List[Task]:
    """Get all tasks for a service."""
    ecs = boto3.client("ecs")
    
    # List task ARNs
    response = ecs.list_tasks(
        cluster=cluster,
        serviceName=service
    )
    
    task_arns = response.get("taskArns", [])
    
    if not task_arns:
        return []
    
    # Describe tasks
    response = ecs.describe_tasks(
        cluster=cluster,
        tasks=task_arns
    )
    
    return cast(List[Task], response["tasks"])


def find_container_name(
    task: Task, task_definition: TaskDefinition, specified_container: Optional[str]
) -> str:
    """Find the appropriate container name to connect to."""
    containers = task.get("containers", [])
    container_names = {container["name"] for container in containers}
    
    # If a container name was specified, verify it exists
    if specified_container:
        if specified_container not in container_names:
            raise ValueError(
                f"Container '{specified_container}' not found in task. "
                f"Available containers: {', '.join(sorted(container_names))}"
            )
        return specified_container
    
    # If there's only one container, use that
    if len(containers) == 0:
        raise ValueError("No containers found in task")
    elif len(containers) == 1:
        return containers[0]["name"]
    
    # Find essential containers from task definition
    essential_containers = {
        container_def["name"]
        for container_def in task_definition.get("containerDefinitions", [])
        if container_def.get("essential", False) and container_def["name"] in container_names
    }
    
    # If there's only one essential container, use that
    if len(essential_containers) == 1:
        return next(iter(essential_containers))
    
    # Can't determine container automatically
    raise ValueError(
        f"Container name must be specified with -c because it cannot be "
        f"automatically determined. Available containers: {', '.join(sorted(container_names))}"
    )


def find_task(tasks: List[Task], task_id: Optional[str]) -> Task:
    """Find a specific task or the most recently started task."""
    if not tasks:
        raise ValueError("No running tasks found")
    
    # If a specific task ID was specified, find it
    if task_id:
        for task in tasks:
            if task["taskArn"].split("/")[-1] == task_id:
                return task
        
        task_ids = [t["taskArn"].split("/")[-1] for t in tasks]
        raise ValueError(
            f"Task '{task_id}' not found. Available tasks: {', '.join(sorted(task_ids))}"
        )
    
    # Otherwise, find the most recently started task
    def get_start_time(task):
        # Get the startedAt value, defaulting to None if not present
        started_at = task.get("startedAt")
        
        # If it's None or missing, use minimum datetime
        if started_at is None:
            return datetime.datetime.min
        
        # If it's already a datetime object, use it directly
        if isinstance(started_at, datetime.datetime):
            return started_at
        
        # If it's a string, try to parse it 
        if isinstance(started_at, str):
            try:
                if started_at.endswith("Z"):
                    started_at = started_at.replace("Z", "+00:00")
                return datetime.datetime.fromisoformat(started_at)
            except ValueError:
                # If we can't parse it, use minimum datetime
                return datetime.datetime.min
        
        # For any other type, use minimum datetime
        return datetime.datetime.min
    
    return max(tasks, key=get_start_time)


def execute_interactive_command(
    cluster: str, task_id: str, container: str, command: str
) -> None:
    """Execute an interactive command in a container using AWS CLI."""
    print(f"Connecting to container '{container}' in task '{task_id}' on cluster '{cluster}'...")
    print(f"Executing command: {command}")
    
    aws_cmd = [
        "aws", "ecs", "execute-command",
        "--cluster", cluster,
        "--task", task_id,
        "--container", container,
        "--command", command,
        "--interactive",
    ]
    
    try:
        subprocess.run(aws_cmd, check=True)
    except KeyboardInterrupt:
        print("\nCommand interrupted with CTRL+C. The shell session has been terminated.")
        print("You can run the script again to start a new session.")
        return
    except subprocess.CalledProcessError as e:
        print(f"Failed to execute command: {e}")
        if e.stderr:
            print(f"Error details: {e.stderr}")
        sys.exit(1)


def display_clusters_table() -> None:
    """Display a table of all clusters, services, and their details."""
    clusters = get_all_clusters()
    if not clusters:
        print("No ECS clusters found")
        return
    
    rows = []
    headers = ["Cluster Name", "Service Name", "Running Tasks", "Task Definition"]
    
    for cluster in sorted(clusters):
        try:
            services = get_services_for_cluster(cluster)
            for service in sorted(services):
                try:
                    service_details = get_service_details(cluster, service)
                    task_definition = service_details["taskDefinition"].split("/")[-1]
                    running_count = service_details.get("runningCount", 0)
                    
                    rows.append([cluster, service, running_count, task_definition])
                except Exception as e:
                    rows.append([cluster, service, f"Error: {str(e)}", ""])
        except Exception as e:
            rows.append([cluster, f"Error listing services: {str(e)}", "", ""])
    
    # Print the table
    try:
        import tabulate
        print(tabulate.tabulate(rows, headers=headers, tablefmt="grid"))
    except ImportError:
        # Fallback to simple printing if tabulate is not available
        col_widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
        
        # Print header
        header_fmt = " | ".join(f"{{:{w}}}" for w in col_widths)
        print(header_fmt.format(*headers))
        print("-" * (sum(col_widths) + 3 * (len(headers) - 1)))
        
        # Print rows
        row_fmt = " | ".join(f"{{:{w}}}" for w in col_widths)
        for row in rows:
            print(row_fmt.format(*[str(item) for item in row]))


def main() -> None:
    """Main function."""
    args = parse_args()
    
    # If no cluster and service are specified, show the cluster/service table
    if not args.cluster_name or not args.service_name:
        display_clusters_table()
        return
    
    try:
        # Get the tasks for the service
        if not (tasks := get_tasks_for_service(args.cluster_name, args.service_name)):
            print(f"No running tasks found for service '{args.service_name}' in cluster '{args.cluster_name}'")
            return
        
        # Find the appropriate task
        task = find_task(tasks, args.task)
        task_id = task["taskArn"].split("/")[-1]
        
        # Get task definition
        task_definition = get_task_definition(task["taskDefinitionArn"])
        
        # Find the appropriate container
        container_name = find_container_name(task, task_definition, args.container)
        
        # Execute the command
        execute_interactive_command(args.cluster_name, task_id, container_name, args.shell)
        
    except ClientError as e:
        print(f"AWS Error: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()