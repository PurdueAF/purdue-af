helm upgrade --install cmshub-ssh \
   --repo https://yuvipanda.github.io/jupyterhub-ssh/ jupyterhub-ssh \
   --version 0.0.1-0.dev.git.142.h402a3d6 \
   --values values.yaml \
   --namespace cms \
   --post-renderer ./kustomize