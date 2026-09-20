# Deployment runbook

## Overview
Every service of the billing platform ships as a Docker image. The images are built by the CI pipeline, pushed to the internal registry and pulled by the production hosts. This runbook describes how to build, run, roll back and clean up those Docker images, and what to check when a container misbehaves.

## Building the Docker image
The Dockerfile uses a multi-stage build. The first stage installs the build tools and compiles the dependencies, the second stage copies only the finished application into a slim base image. Always build with the git commit hash as the tag, never with the moving tag latest, so that every running container can be traced back to one commit. A Docker build that needs more than ten minutes usually means the layer cache was lost.

## Running with Docker Compose
Production hosts use Docker Compose with one file per environment. Start the stack with the up command in detached mode and check that every container reports as healthy before you send traffic to the host. The Compose file pins the image tags, the memory limits and the restart policy of each container, so a host that reboots brings the same containers back.

## Container health checks
Each container defines a health check that calls the service's own health endpoint every thirty seconds. After three failed checks Docker marks the container unhealthy and the restart policy replaces it. A container that is restarted more than five times in ten minutes should be investigated by hand instead of being left in a restart loop.

## Rolling back a Docker deployment
To roll back, change the image tag in the environment's Compose file to the previous commit hash and run the up command again. Docker only recreates the containers whose image changed. Database migrations are not rolled back automatically: check the release notes for a down migration before you roll back a release that changed the schema.

## Logs and Docker volumes
Containers write their logs to standard output, and the Docker logging driver ships them to the central log store. Persistent data lives in named Docker volumes, never inside the container's own filesystem. Back up the volumes nightly and test a restore every quarter.

## Cleaning up old Docker images
Old images fill the disk of a production host within weeks. A weekly job removes images that no running container uses and that are older than fourteen days. Never remove images by hand on a host that is in the middle of a deployment.

## A container that will not start
Read the container logs first, then inspect the exit code. Exit code 137 means the container was killed for using too much memory, and exit code 1 usually means a configuration error such as a missing environment variable. Try to start the same image locally with the production configuration before you change the image itself.
