Login options and user naming rules
======================================

The user authentication is implemented using **CILogon**. Three login options are available: 
..
* Purdue University account (BoilerKey)
* CERN account (CMS users only)
* FNAL account

For Purdue and CERN users, the usernames are compared against a list of allowed users
in order to prevent abuse of resources.
For Purdue, the list of allowed users is defined as all users who have access to the Hammer cluster,
while for CERN the allowed users are required to be members of CMS VO.

The same person using different login credentials is treated as different users,
which means that they will be assigned separate work areas and storage volumes for each login method.

At login, the username and the hostname are constructed as follows:

* Local Purdue account: ``dkondra@purdue-af-1``
* CERN account: ``dkondrat-cern@purdue-af-2``
* FNAL account: ``dkondrat-fnal@purdue-af-3``

The username starts with the username taken from login credentials,
which may or may not be the same for different accounts, and even for different people.
In order to avoid naming conflicts, CERN and FNAL usernames are amended by ``-cern`` and ``-fnal`` suffixes,
respectively, while Purdue usernames are left unchanged. Since within the same institution usernames are unique,
this ensures that a new user cannot accidentally get access to another user's data.
Since every user is assigned a dedicated kubernetes pod which is not shared with other users,
the hostname must also be unique. This is done by constructing the hostnames from unique user IDs taken
from the user database.