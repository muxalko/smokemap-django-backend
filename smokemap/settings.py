"""
Django settings for smokemap project.

Generated by 'django-admin startproject' using Django 4.1.3.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/4.1/ref/settings/
"""
from datetime import timedelta
from pathlib import Path
import os
# load .env
from dotenv import load_dotenv
load_dotenv()

# set gdal library path for django to find it
from glob import glob

if os.getenv('SETTINGS_MODE') == 'local':
    print("DEVELOPMENT MODE !!! - Hello from " + str(os.getpid()))
    GDAL_LIBRARY_PATH=glob('/usr/lib/libgdal.so.*')[0]
    GEOS_LIBRARY_PATH=glob('/usr/lib/x86_64-linux-gnu/libgeos_c.so.*')[0]
    # SECURITY WARNING: don't run with debug turned on in production!
    DEBUG = True
    # ALLOWED_HOSTS=['*']
    CORS_ALLOW_HEADERS = ['x-csrftoken','content-type']
    # CORS_ORIGIN_ALLOW_ALL = True
    CORS_ALLOW_CREDENTIALS = True
    ALLOWED_HOSTS = [
        'localhost', # retrieve schema wwhen developing
        'smokemap.org'
        ]
    CORS_ALLOWED_ORIGINS = [
        "http://localhost:3000",
        'http://smokemap.org:3000'
    ]
    CSRF_TRUSTED_ORIGINS = [
        'http://smokemap.org:3000',
    ]

    # used with allauth/dj-rest-auth
    SIMPLE_JWT = {
        "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
        "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
        "ROTATE_REFRESH_TOKENS": False,
        "BLACKLIST_AFTER_ROTATION": False,
        "UPDATE_LAST_LOGIN": True,
        "SIGNING_KEY": "complexsigningkey",  # generate a key and replace me
        "ALGORITHM": "HS512",
    }

    # used with graphene auth
    # Defines JWT settings and auth backends
    GRAPHQL_JWT = {
        # 'JWT_PAYLOAD_HANDLER': 'app.utils.jwt_payload',
        'JWT_AUTH_HEADER_PREFIX': 'Bearer',
        'JWT_VERIFY_EXPIRATION': True,
        'JWT_LONG_RUNNING_REFRESH_TOKEN': True,
        'JWT_EXPIRATION_DELTA': timedelta(minutes=5),
        'JWT_REFRESH_EXPIRATION_DELTA': timedelta(days=7),    
        'JWT_SECRET_KEY': os.environ['DJANGO_SECRET_KEY'],
        'JWT_ALGORITHM': 'HS256',
        'JWT_COOKIE_SECURE': False,
        # 'JWT_COOKIE_DOMAIN': 'smokemap.org',
        'JWT_COOKIE_SAMESITE': 'Lax'
    }

    # Graphql settings
    GRAPHENE = {
        "SCHEMA": "backend.schema.schema",
        "MIDDLEWARE": [
            "graphql_jwt.middleware.JSONWebTokenMiddleware",
        ],
    }
# try to detect vercel environment
elif os.getenv("$VERCEL_GIT_COMMIT_REF") == 'staging':
    print("STAGING MODE !!! - Hello from " + str(os.getpid()))
    #GDAL_LIBRARY_PATH = ".vercel/builders/node_modules/vercel-python-gis/dist/files/libgdal.so"
    #GEOS_LIBRARY_PATH = ".vercel/builders/node_modules/vercel-python-gis/dist/files/libgeos_c.so.1"
    GDAL_LIBRARY_PATH = "libgdal.so"
    GEOS_LIBRARY_PATH = "libgeos_c.so.1"
    DEBUG = False
    ALLOWED_HOSTS = ['.vercel.app']
    CORS_ALLOWED_ORIGINS = [
        # vercel preview/development/staging
        'https://smokemap-webapp-git-staging-muxalko.vercel.app'
    ]
    # for matching preview and development autogenerated fqdns !
    # CORS_ALLOWED_ORIGIN_REGEXES = [
    #     r"staging-muxalko\.vercel\.app$",
    # ]

    CORS_ALLOW_HEADERS = ['x-csrftoken','content-type']

    CORS_ALLOW_CREDENTIALS = True

    CORS_ALLOWED_ORIGINS = [
        'https://smokemap-webapp-git-staging-muxalko.vercel.app'
    ]

    CSRF_TRUSTED_ORIGINS = [
        'https://smokemap-webapp-git-staging-muxalko.vercel.app'
    ]

    SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "UPDATE_LAST_LOGIN": True,
    "SIGNING_KEY": "complexsigningkey",  # generate a key and replace me
    "ALGORITHM": "HS512",
    }

    # Graphql settings
    GRAPHENE = {
        "SCHEMA": "backend.schema.schema",
        "RELAY_CONNECTION_MAX_LIMIT": 100,
        'MIDDLEWARE': [
            "graphql_jwt.middleware.JSONWebTokenMiddleware",
            # "backend.graphql.middleware.DisableIntrospectionMiddleware",
        ],
    }

    # used with allauth/dj-rest-auth
    # SIMPLE_JWT = {
    #     "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    #     "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    #     "ROTATE_REFRESH_TOKENS": False,
    #     "BLACKLIST_AFTER_ROTATION": False,
    #     "UPDATE_LAST_LOGIN": True,
    #     "SIGNING_KEY": "complexsigningkey",  # generate a key and replace me
    #     "ALGORITHM": "HS512",
    # }

    # used with graphene auth
    # Defines JWT settings and auth backends
    GRAPHQL_JWT = {
        # 'JWT_PAYLOAD_HANDLER': 'app.utils.jwt_payload',
        'JWT_AUTH_HEADER_PREFIX': 'Bearer',
        'JWT_VERIFY_EXPIRATION': True,
        'JWT_LONG_RUNNING_REFRESH_TOKEN': True,
        'JWT_EXPIRATION_DELTA': timedelta(minutes=5),
        'JWT_REFRESH_EXPIRATION_DELTA': timedelta(days=7),    
        'JWT_SECRET_KEY': os.environ['DJANGO_SECRET_KEY'],
        'JWT_ALGORITHM': 'HS256',
        'JWT_COOKIE_SECURE': False,
        # 'JWT_COOKIE_DOMAIN': 'smokemap.org',
        'JWT_COOKIE_SAMESITE': 'Lax'
    }

elif os.getenv("$VERCEL_GIT_COMMIT_REF") == 'main':
    print("PRODUCTION MODE !!! - Hello from " + str(os.getpid()))
    #GDAL_LIBRARY_PATH = ".vercel/builders/node_modules/vercel-python-gis/dist/files/libgdal.so"
    #GEOS_LIBRARY_PATH = ".vercel/builders/node_modules/vercel-python-gis/dist/files/libgeos_c.so.1"
    GDAL_LIBRARY_PATH = "libgdal.so"
    GEOS_LIBRARY_PATH = "libgeos_c.so.1"
    DEBUG = False
    ALLOWED_HOSTS = ['.vercel.app']
    CORS_ALLOWED_ORIGINS = [
        # vercel production - main branch
        'https://smokemap.vercel.app',
        'https://smokemap-webapp-muxalko.vercel.app',
    ]
    # for matching preview and development autogenerated fqdns !
    # CORS_ALLOWED_ORIGIN_REGEXES = [
    #     r"muxalko\.vercel\.app$",
    # ]

    CORS_ALLOW_HEADERS = ['x-csrftoken','content-type']

    CORS_ALLOW_CREDENTIALS = False

    CORS_ALLOWED_ORIGINS = [
        'https://smokemap.vercel.app',
    ]

    CSRF_TRUSTED_ORIGINS = [
        'https://smokemap.vercel.app',
    ]

    SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "UPDATE_LAST_LOGIN": True,
    "SIGNING_KEY": "complexsigningkey",  # generate a key and replace me
    "ALGORITHM": "HS512",
    }

    # Graphql settings
    GRAPHENE = {
        "SCHEMA": "backend.schema.schema",
        "RELAY_CONNECTION_MAX_LIMIT": 100,
        'MIDDLEWARE': [
            "graphql_jwt.middleware.JSONWebTokenMiddleware",
            "backend.graphql.middleware.DisableIntrospectionMiddleware",
        ],
    }

    # used with allauth/dj-rest-auth
    # SIMPLE_JWT = {
    #     "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    #     "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    #     "ROTATE_REFRESH_TOKENS": False,
    #     "BLACKLIST_AFTER_ROTATION": False,
    #     "UPDATE_LAST_LOGIN": True,
    #     "SIGNING_KEY": "complexsigningkey",  # generate a key and replace me
    #     "ALGORITHM": "HS512",
    # }

    # used with graphene auth
    # Defines JWT settings and auth backends
    GRAPHQL_JWT = {
        # 'JWT_PAYLOAD_HANDLER': 'app.utils.jwt_payload',
        'JWT_AUTH_HEADER_PREFIX': 'Bearer',
        'JWT_VERIFY_EXPIRATION': True,
        'JWT_LONG_RUNNING_REFRESH_TOKEN': True,
        'JWT_EXPIRATION_DELTA': timedelta(minutes=5),
        'JWT_REFRESH_EXPIRATION_DELTA': timedelta(days=7),    
        'JWT_SECRET_KEY': os.environ['DJANGO_SECRET_KEY'],
        'JWT_ALGORITHM': 'HS256',
        'JWT_COOKIE_SECURE': False,
        # 'JWT_COOKIE_DOMAIN': 'smokemap.org',
        'JWT_COOKIE_SAMESITE': 'Lax'
    }

print("GDAL_LIBRARY_PATH="+GDAL_LIBRARY_PATH)
print("GEOS_LIBRARY_PATH="+GEOS_LIBRARY_PATH)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY','')

# AWS Params
# S3 Storage Configurations for retreiving presigned post url
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID','')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY','')
AWS_STORAGE_BUCKET_NAME = os.environ.get('AWS_STORAGE_BUCKET_NAME','')
AWS_S3_REGION_NAME = os.environ.get('AWS_REGION','')

# Application definition
INSTALLED_APPS = [
    "django.contrib.sites", #django-allauth depends on Django's "sites" framework
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles', # Required for GraphiQL
    'corsheaders', # CORS support
    'graphene_django', # graphql
    'graphql_jwt.refresh_token.apps.RefreshTokenConfig',
    'rest_framework', # Django Rest Framework
    'rest_framework.authtoken', # Token authentication
    'rest_framework_simplejwt', # JSON Web 
    'rest_framework_gis', # Geo addition
    'django.contrib.gis', # 
    # 'django_tiles_gl',
    "allauth",
    "allauth.account",
    "allauth.socialaccount",  # add if you want social authentication
    "allauth.socialaccount.providers.google",
    "dj_rest_auth",
    "dj_rest_auth.registration",
    # our application
    'backend',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = 'smokemap.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'smokemap.wsgi.app'

# Database
# https://docs.djangoproject.com/en/4.1/ref/settings/#databases
# Note: Django modules for using databases are not support in serverless
# environments like Vercel. You can use a database over HTTP, hosted elsewhere.

# https://stackoverflow.com/questions/12538510/getting-databaseoperations-object-has-no-attribute-geo-db-type-error-when-do
# This engine fails with: AttributeError: 'DatabaseOperations' object has no attribute 'geo_db_type'
#ENGINE = 'django.db.backends.postgresql'
ENGINE = 'django.contrib.gis.db.backends.postgis'
HOST = os.environ.get('POSTGRES_HOST','127.0.0.1')
PORT = os.environ.get('POSTGRES_PORT','5432')
USER = os.environ.get('POSTGRES_USER','postgres')
PASS = os.environ.get('POSTGRES_PASSWORD','postgres')
NAME = os.environ.get('POSTGRES_DB','postgres')
OPTIONS = os.environ.get('POSTGRES_OPTIONS','-c search_path=smokemap')
DATABASES = {
     'default': {
        'ENGINE': ENGINE,
        'NAME': NAME,
        'USER': USER,
        'PASSWORD': PASS,
        'HOST': HOST,
        'PORT': PORT,
        'OPTIONS': {
            'options': OPTIONS
            },
    }
}

# where are the files for tile server located
# MBTILES_DATABASE = BASE_DIR / "data" / "places.mbtiles"

# Password validation
# https://docs.djangoproject.com/en/4.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.1/howto/static-files/

STATIC_URL = 'static/'
STATICFILES_DIRS = os.path.join(BASE_DIR, 'static'),
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles_build', 'static')

# MEDIA_URLS ='/media/'
# MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default primary key field type
# https://docs.djangoproject.com/en/4.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'



# User settings
AUTH_USER_MODEL = "backend.CustomUser"
ACCOUNT_ADAPTER = 'backend.adapter.CustomAccountAdapter'

# used to redirect unauthenticated users 
LOGIN_URL = '/api-auth/login/'

SITE_ID = 1

ACCOUNT_AUTHENTICATION_METHOD = 'email'
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_EMAIL_VERIFICATION = "none"

REST_AUTH = {
    # 'LOGIN_SERIALIZER': 'dj_rest_auth.serializers.LoginSerializer',
    # 'TOKEN_SERIALIZER': 'dj_rest_auth.serializers.TokenSerializer',
    # 'JWT_SERIALIZER': 'dj_rest_auth.serializers.JWTSerializer',
    # 'JWT_SERIALIZER_WITH_EXPIRATION': 'dj_rest_auth.serializers.JWTSerializerWithExpiration',
    # 'JWT_TOKEN_CLAIMS_SERIALIZER': 'rest_framework_simplejwt.serializers.TokenObtainPairSerializer',
   
    # 'USER_DETAILS_SERIALIZER': 'dj_rest_auth.serializers.UserDetailsSerializer',
     'USER_DETAILS_SERIALIZER': 'backend.serializers.CustomUserDetailsSerializer',
    
    # 'PASSWORD_RESET_SERIALIZER': 'dj_rest_auth.serializers.PasswordResetSerializer',
    # 'PASSWORD_RESET_SERIALIZER': 'backend.serializers.CustomPasswordResetSerializer',
    
    # 'PASSWORD_RESET_CONFIRM_SERIALIZER': 'dj_rest_auth.serializers.PasswordResetConfirmSerializer',
    # 'PASSWORD_CHANGE_SERIALIZER': 'dj_rest_auth.serializers.PasswordChangeSerializer',

    # 'REGISTER_SERIALIZER': 'dj_rest_auth.registration.serializers.RegisterSerializer',
    # 'REGISTER_SERIALIZER': 'backend.serializers.CustomRegisterSerializer',

    # 'REGISTER_PERMISSION_CLASSES': ('rest_framework.permissions.AllowAny',),

    # 'TOKEN_MODEL': 'rest_framework.authtoken.models.Token',
    # 'TOKEN_CREATOR': 'dj_rest_auth.utils.default_create_token',

    # 'PASSWORD_RESET_USE_SITES_DOMAIN': False,
    # 'OLD_PASSWORD_FIELD_ENABLED': False,
    # 'LOGOUT_ON_PASSWORD_CHANGE': False,
    # 'SESSION_LOGIN': True,
    'USE_JWT': True, # to use refresh/verify routes

    # 'JWT_AUTH_COOKIE': None,
    # 'JWT_AUTH_REFRESH_COOKIE': None,
    # 'JWT_AUTH_REFRESH_COOKIE_PATH': '/',
    # 'JWT_AUTH_SECURE': False,
    'JWT_AUTH_HTTPONLY': False, # refresh_token will not be sent if set to True
    # 'JWT_AUTH_SAMESITE': 'Lax',
    'JWT_AUTH_RETURN_EXPIRATION': True,
    # 'JWT_AUTH_COOKIE_USE_CSRF': False,
    # 'JWT_AUTH_COOKIE_ENFORCE_CSRF_ON_UNAUTHENTICATED': False,
}

# DRF Settings
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        #'rest_framework.permissions.AllowAny',
        'rest_framework.permissions.IsAuthenticated',
    ],
     'DEFAULT_AUTHENTICATION_CLASSES': [
        # 'rest_framework.authentication.SessionAuthentication',
        # 'rest_framework.authentication.BasicAuthentication',
        # 'rest_framework.authentication.TokenAuthentication',
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
}

AUTHENTICATION_BACKENDS = [
    "graphql_jwt.backends.JSONWebTokenBackend",
    # Needed to login by username in Django admin, regardless of `allauth`
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
    ]

# SOCIALACCOUNT_PROVIDERS = {
#     "google": {
#         "APP": {
#             "client_id": os.environ.get('AUTH_GOOGLE_ID',''),  
#             "secret": os.environ.get('AUTH_GOOGLE_SECRET',''),        
#             "key": "",                               # leave empty
#         },
#         "SCOPE": [
#             "profile",
#             "email",
#         ],
#         "AUTH_PARAMS": {
#             "access_type": "online",
#         },
#         "VERIFIED_EMAIL": True,
#     },
# }