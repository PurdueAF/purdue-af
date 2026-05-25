FROM geddes-registry.rcac.purdue.edu/docker-hub-cache/sslhep/servicex_science_image_combined_root:5.7.2-6.38.04

USER root
# Match upstream combined_root: packages live in the pixi env.
RUN bash -lc 'pixi add --pypi coffea==2026.4.0' \
    && bash -lc 'install -m 755 "$(pixi run which python)" /usr/local/bin/python' \
    && chmod -R g+rwX /.pixi
# Sidecar runs as celery (uid 1001, gid 0) and creates /servicex/output trees.
# Align science container user so it can write scratch outputs and abc.log.
RUN usermod -u 1001 -g 0 output \
    && chown -R output:0 /home/output \
    && chmod -R g+rwX /home/output

USER output
