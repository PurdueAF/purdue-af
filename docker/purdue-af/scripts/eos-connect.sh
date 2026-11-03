#!/bin/bash
# Sourced through the eos-connect alias: it returns, and defines no functions in the user's shell.
_eos_connect() {
	local krb_ticket cern_username first_letter eos_directory

	echo ""
	echo ""
	echo "------------------------ Connecting to CERN EOS ------------------------"
	echo ""

	krb_ticket=$(klist 2>/dev/null | grep FILE | cut -d ':' -f3)
	if [[ -z "$krb_ticket" ]]; then
		echo " > Kerberos ticket not found."
		echo ""
		echo " > Let's start with initializing the Kerberos ticket."

		# A CERN login carries the CERN username; any other is asked for it.
		if [[ "$USER" == *"-cern" ]]; then
			cern_username=$(echo "$USER" | cut -d '-' -f1)
		else
			echo " > What is your CERN username? Enter below:"
			read -r -p " > " cern_username
		fi

		kinit "$cern_username@CERN.CH"

		krb_ticket=$(klist 2>/dev/null | grep FILE | cut -d ':' -f3)
		if [[ -z "$krb_ticket" ]]; then
			echo " > Kerberos authentication failed!"
			echo ""
			return 1
		fi
		echo " > Kerberos authentication complete!"
		echo ""
	else
		echo " > Kerberos ticket found: $krb_ticket"
		cern_username=$(klist | grep Default | cut -d ' ' -f3 | cut -d '@' -f1)
	fi

	if [ -L "$HOME/eos-cern" ]; then
		echo " > Symlink $HOME/eos-cern already exists."
		echo ""
	else
		echo " > Symlink $HOME/eos-cern doesn't exist yet."
		first_letter=$(echo "$cern_username" | cut -b 1)
		eos_directory="/eos/cern/home-$first_letter/$cern_username/"
		ln -s "$eos_directory" "$HOME/eos-cern" 2>/dev/null
		echo " > Creating symlink $HOME/eos-cern that points to $eos_directory"
		echo ""
		echo " > The directory 'eos-cern' should appear in the file browser in a few seconds."
		echo " > The interaction with CERN EOS may be slow at first."
		echo ""
		echo " > If the file browser shows 'eos-cern' as a file rather than a directory,"
		echo " > try restarting the session, and then run the eos-connect command again."
		echo ""
	fi

	echo "--------------------------------- Done! ---------------------------------"
	echo ""
	echo ""
}

_eos_connect || {
	unset -f _eos_connect
	return 1
}
unset -f _eos_connect
