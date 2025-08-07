krb_ticket=$(klist 2>/dev/null | grep FILE | cut -d ':' -f3)
link_exists=$(ls $HOME/eos-cern 2>/dev/null)
if [ -L $HOME/eos-cern ]; then link_exists="true"; else link_exists="false"; fi

echo ""
echo ""
echo "------------------------ Connecting to CERN EOS ------------------------"
echo ""

if [[ $krb_ticket = "" ]]; then
	echo " > Kerberos ticket not found."
	if [[ $link_exists = "false" ]]; then
		echo " > Symlink $HOME/eos-cern doesn't exist yet."
		echo ""
		echo " > Let's start with initializing the Kerberos ticket."

		# If CERN account was used to log in, reuse the username
		# otherwise, prompt to enter the username
		if [[ $USER == *"-cern" ]]; then
			cern_username=$(echo $USER | cut -d '-' -f1)
		else
			echo " > What is your CERN username? Enter below:"
			read -p " > " cern_username
		fi

		kinit $cern_username@CERN.CH

		# Check if Kerberos authentication succeeded
		krb_ticket=$(klist 2>/dev/null | grep FILE | cut -d ':' -f3)
		if [[ $krb_ticket = "" ]]; then
			echo " > Kerberos authentication failed!"
			echo ""
			return 1
			else:
			echo " > Kerberos authentication complete!"
			echo ""
		fi

		# Create a symlink
		first_letter=$(echo $cern_username | cut -b 1)
		eos_directory=/eos/cern/home-$first_letter/$cern_username/
		ln -s $eos_directory $HOME/eos-cern 2>/dev/null
		echo " > Creating symlink $HOME/eos-cern that points to $eos_directory"
		echo ""

		echo " > The directory 'eos-cern' should appear in the file browser in a few seconds."
		echo " > The interaction with CERN EOS may be slow at first."
		echo ""
		echo " > If the file browser shows 'eos-cern' as a file rather than a directory,"
		echo " > try restarting the session, and then run the eos-connect command again."
	else
		echo " > Symlink $HOME/eos-cern already exists."
		echo ""
		echo " > It is unlikely that the file browser will show the eos-cern link correctly"
		echo " > in this session."
		echo " > Please restart the session, and then run the eos-connect command again."
		echo " > The broken link will be automatically deleted when the session is closed."
		echo ""
		echo "--------------- Please restart the session and try again! ---------------"
		echo ""
		return
	fi

else
	echo " > Kerberos ticket found: $krb_ticket"

	# Extract username from Kerberos ticket
	cern_username=$(klist | grep Default | cut -d ' ' -f3 | cut -d '@' -f1)

	if [[ $link_exists = "false" ]]; then
		echo " > Symlink $HOME/eos-cern doesn't exist yet."

		# Create symlink
		first_letter=$(echo $cern_username | cut -b 1)
		eos_directory=/eos/cern/home-$first_letter/$cern_username/
		ln -s $eos_directory $HOME/eos-cern 2>/dev/null
		echo " > Creating symlink $HOME/eos-cern that points to $eos_directory"
		echo ""
	else
		echo " > Symlink $HOME/eos-cern already exists."
		echo ""
	fi

	echo " > If the file browser shows 'eos-cern' as a file rather than a directory,"
	echo " > try restarting the session, and then run the eos-connect command again."
fi
echo ""
echo "--------------------------------- Done! ---------------------------------"
echo ""
echo ""
