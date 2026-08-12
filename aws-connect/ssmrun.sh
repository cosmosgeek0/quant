#!/bin/bash
# ssmrun.sh <script-file> [seconds] — run a script on the cosmos host over SSM Session Manager.
# The IAM operator may StartSession on the instance only (no SendCommand, no documents),
# so this drives the default shell session over stdin and captures the transcript.
set -uo pipefail
export PATH="$PATH:/usr/local/sessionmanagerplugin/bin"
export AWS_PAGER=""
SCRIPT="${1:?usage: ssmrun.sh <script-file> [seconds]}"
WAIT="${2:-45}"
TARGET=i-0e87fe0e7ba2063a9
REGION=ap-south-1
{
  sleep 3
  cat "$SCRIPT"
  printf '\necho __SSMRUN_DONE__\n'
  sleep "$WAIT"
  printf 'exit\n'
  sleep 2
} | aws ssm start-session --region "$REGION" --target "$TARGET" 2>&1
