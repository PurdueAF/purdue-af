#!/bin/bash

source /opt/conda/bin/activate /depot/cms/kernels/python3

cd /app/analysis-grand-challenge/analyses/cms-open-data-ttbar/

rm -rf /work/projects/purdue-af/agc/metrics/event_rate.txt

echo "Starting Dask Gateway cluster..."
python /app/start_dask_cluster.py &
DASK_PID=$!
sleep 30

echo "Running ttbar_analysis_pipeline.py ..."
output=$(python /app/analysis-grand-challenge/analyses/cms-open-data-ttbar/ttbar_analysis_pipeline.py)
echo "Complete!"

event_rate=$(echo "$output" | grep -oP 'event rate per worker \(pure processtime\): \K[0-9.]+')
echo "Event rate: " $event_rate "kHz"
echo $event_rate > /work/projects/purdue-af/agc/metrics/event_rate.txt

echo "Stopping Dask Gateway cluster..."
kill $DASK_PID
python /app/stop_dask_cluster.py
