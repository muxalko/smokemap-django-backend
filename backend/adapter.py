from allauth.account.adapter import DefaultAccountAdapter
from django.contrib.auth.models import Group

class CustomAccountAdapter(DefaultAccountAdapter):

    def save_user(self, request, user, form, commit=False):
        user = super().save_user(request, user, form, commit)
        data = form.cleaned_data
        role = data.get('role')
        user.role = 'admin'

        user.save()
        print(dir(user))
        try: 
            group = Group.objects.get(name=role)
            request.user.groups.add(group)
           
        except Exception as e:
            print("Role not found"),
        

        return user