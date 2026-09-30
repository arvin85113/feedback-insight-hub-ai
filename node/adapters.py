from allauth.account.adapter import DefaultAccountAdapter


class NodeAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Staff join a node by invitation from the owner, never by self sign-up.
        return False
