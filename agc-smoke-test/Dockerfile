FROM geddes-registry.rcac.purdue.edu/cms/cmsaf-alma8:0.9.6

ENV PATH /opt/conda/bin:$PATH

USER root

RUN git clone https://github.com/kondratyevd/analysis-grand-challenge.git /app/analysis-grand-challenge

WORKDIR /app/analysis-grand-challenge

COPY run_agc.sh start_dask_cluster.py stop_dask_cluster.py /app/
COPY nanoaod_inputs.json /app/analysis-grand-challenge/analyses/cms-open-data-ttbar/
RUN chmod -R 777 /app/

USER jovyan

CMD ["/bin/bash", "/app/run_agc.sh"]