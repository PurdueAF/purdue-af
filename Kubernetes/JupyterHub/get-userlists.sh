# Get list of Purdue users
ldapsearch -x host=hammer.rcac.purdue.edu | grep uid: | cut -d " " -f2 > purdue-auth.txt

# Get list of CERN CMS users
wget --no-check-certificate --certificate=$HOME/.globus/usercert.pem --private-key=$HOME/.globus/userkey.pem https://voms2.cern.ch:8443/voms/cms/services/VOMSCompatibility?method=getGridmapUsers -O grid-mapfile.xml

xmllint --format grid-mapfile.xml |grep 'getGridmapUsersReturn xsi:type' |cut -d '>' -f2|cut -d '<' -f1 | grep "OU=Users" | cut -d "=" -f6 | cut -d "/" -f1 > cern-auth.txt

