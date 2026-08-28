#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root" >&2
  exit 77
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
config_source="${repo_root}/deploy/aws/monitoring/cloudwatch-agent.json"
config_target="/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json"

if [[ ! -r "${config_source}" ]]; then
  echo "CloudWatch Agent configuration is missing: ${config_source}" >&2
  exit 78
fi

arch="$(dpkg --print-architecture)"
case "${arch}" in
  amd64|arm64) ;;
  *) echo "Unsupported architecture: ${arch}" >&2; exit 78 ;;
esac

package_url="https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/${arch}/latest/amazon-cloudwatch-agent.deb"
package_file="$(mktemp --suffix=.deb)"
trap 'rm -f "${package_file}"' EXIT

curl --fail --show-error --silent --location "${package_url}" --output "${package_file}"
dpkg --install "${package_file}"
install -o root -g root -m 0644 "${config_source}" "${config_target}"

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s -c "file:${config_target}"
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
