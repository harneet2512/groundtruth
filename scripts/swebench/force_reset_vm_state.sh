#!/bin/bash
# Nuclear cleanup: kill all sweagent/scrapers/docker astropy containers, nuke stale OUTDIRs.
ps -ef | awk '/sweagent/ && !/awk/ && !/grep/ {print $2}' | xargs -r kill -9 2>/dev/null
ps -ef | awk '/gt_telemetry/ && !/awk/ && !/grep/ {print $2}' | xargs -r kill -9 2>/dev/null
sleep 3
docker ps --format '{{.ID}} {{.Names}}' | awk '/astropy/{print $1}' | xargs -r docker kill >/dev/null 2>&1
docker ps -a --format '{{.ID}} {{.Names}}' | awk '/astropy/{print $1}' | xargs -r docker rm -f >/dev/null 2>&1
sleep 3
echo "sweagent_remaining=$(ps -ef | awk '/sweagent run-batch/ && !/awk/ && !/grep/' | wc -l)"
echo "scraper_remaining=$(ps -ef | awk '/gt_telemetry_scraper/ && !/awk/ && !/grep/' | wc -l)"
echo "astropy_docker_running=$(docker ps --format '{{.Names}}' | grep -c astropy)"
echo "astropy_docker_all=$(docker ps -a --format '{{.Names}}' | grep -c astropy)"
rm -rf /tmp/official_nolsp
rm -rf /tmp/gt_lane_b_lsp_readiness/probe_resynced_1776842189
rm -rf /tmp/gt_lane_b_lsp_readiness/probe_resynced_1776842927
echo "outdir_check=$(ls -d /tmp/official_nolsp 2>&1)"
uptime
