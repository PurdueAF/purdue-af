FROM geddes-registry.rcac.purdue.edu/docker-hub-cache/sslhep/servicex_science_image_combined_root:5.7.2-6.38.04

USER root
# Match upstream combined_root: packages live in the pixi env, not a bare pip install.
RUN bash -lc 'pixi add --pypi coffea==2026.4.0' \
    && bash -lc 'install -m 755 "$(pixi run which python)" /usr/local/bin/python'

USER output
