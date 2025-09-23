# age-keygen -o ~/.config/sops/age/keys.txt
# cat ~/.config/sops/age/keys.txt | grep 'public key'

# dev
kubectl -n cms-dev create secret generic sops-age \
	--from-file=age.agekey=$HOME/.config/sops/age/keys.txt

# in-place encryption:
# sops -e -i path/to/secret.yaml
