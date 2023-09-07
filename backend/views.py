# backend/views.py
from datetime import datetime

from django.http import HttpResponse
from django.contrib.auth.models import User

#password = User.objects.make_random_password() # 7Gjk2kd4T9
#password = User.objects.make_random_password(length=14) # FTELhrNFdRbSgy
password = User.objects.make_random_password(length=14, allowed_chars='!#$%&()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[]^_abcdefghijklmnopqrstuvwxyz{|}~') # zvk0hawf8m6394

#user.set_password(password)

def index(request):
    now = datetime.now()
    html = f'''
    <html>
        <body>
            <h1>Hello from Vercel!</h1>
            <p>The current time is { now }.</p>
            <p>The generated password is  { password }</p>
            
        </body>
    </html>
    '''
    return HttpResponse(html)