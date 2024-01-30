[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fvercel%2Fexamples%2Ftree%2Fmain%2Fpython%2Fdjango&demo-title=Django%20%2B%20Vercel&demo-description=Use%20Django%204%20on%20Vercel%20with%20Serverless%20Functions%20using%20the%20Python%20Runtime.&demo-url=https%3A%2F%2Fdjango-template.vercel.app%2F&demo-image=https://assets.vercel.com/image/upload/v1669994241/random/django.png)

# Django + Vercel

This example shows how to use Django 4 on Vercel with Serverless Functions using the [Python Runtime](https://vercel.com/docs/concepts/functions/serverless-functions/runtimes/python).

## Demo

https://django-template.vercel.app/

## How it Works

Our Django application, `example` is configured as an installed application in `smokemap/settings.py`:

```python
# smokemap/settings.py
INSTALLED_APPS = [
    # ...
    'backend',
]
```

We allow "\*.vercel.app" subdomains in `ALLOWED_HOSTS`, in addition to 127.0.0.1:

```python
# smokemap/settings.py
ALLOWED_HOSTS = ['127.0.0.1', '.vercel.app']
```

The `wsgi` module must use a public variable named `app` to expose the WSGI application:

```python
# smokemap/wsgi.py
app = get_wsgi_application()
```

The corresponding `WSGI_APPLICATION` setting is configured to use the `app` variable from the `smokemap.wsgi` module:

```python
# smokemap/settings.py
WSGI_APPLICATION = 'smokemap.wsgi.app'
```

There is a single view which renders the current time in `backend/views.py`:

```python
# backend/views.py
from datetime import datetime

from django.http import HttpResponse


def index(request):
    now = datetime.now()
    html = f'''
    <html>
        <body>
            <h1>Hello from Vercel!</h1>
            <p>The current time is { now }.</p>
        </body>
    </html>
    '''
    return HttpResponse(html)
```

This view is exposed a URL through `backend/urls.py`:

```python
# backend/urls.py
from django.urls import path

from backend.views import index


urlpatterns = [
    path('', index),
]
```

Finally, it's made accessible to the Django server inside `smokemap/urls.py`:

```python
# smokemap/urls.py
from django.urls import path, include

urlpatterns = [
    ...
    path('', include('backend.urls')),
]
```

This example uses the Web Server Gateway Interface (WSGI) with Django to enable handling requests on Vercel with Serverless Functions.

## Running Locally

```bash
python manage.py runserver
```

Your Django application is now available at `http://localhost:8000`.

## Running inside VM

```bash
SETTINGS_MODE='local' python manage.py runserver 0.0.0.0:8000

DEVELOPMENT MODE !!! - Hello from 75897
GDAL_LIBRARY_PATH=/usr/lib/libgdal.so.26
GEOS_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libgeos_c.so.1
DEVELOPMENT MODE !!! - Hello from 75898
GDAL_LIBRARY_PATH=/usr/lib/libgdal.so.26
GEOS_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libgeos_c.so.1
Watching for file changes with StatReloader
Performing system checks...

System check identified no issues (0 silenced).
January 24, 2024 - 20:58:47
Django version 4.2.8, using settings 'smokemap.settings'
Starting development server at http://0.0.0.0:8000/
Quit the server with CONTROL-C.

```

The environment variable ```SETTINGS_MODE``` is set to "local" to indicate development mode, used in settings.py 

## One-Click Deploy

Deploy the example using [Vercel](https://vercel.com?utm_source=github&utm_medium=readme&utm_campaign=vercel-examples):

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fvercel%2Fexamples%2Ftree%2Fmain%2Fpython%2Fdjango&demo-title=Django%20%2B%20Vercel&demo-description=Use%20Django%204%20on%20Vercel%20with%20Serverless%20Functions%20using%20the%20Python%20Runtime.&demo-url=https%3A%2F%2Fdjango-template.vercel.app%2F&demo-image=https://assets.vercel.com/image/upload/v1669994241/random/django.png)
