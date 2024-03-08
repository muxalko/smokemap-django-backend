# import_places.py
import os,django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "smokemap.settings")
django.setup()
from backend.models import Place, Address, Request, Category

import pandas as pd
from datetime import datetime
from django.contrib.gis import geos
# import nltk
# from nltk import word_tokenize
# from nltk.util import ngrams
# from collections import defaultdict, Counter
import random
from graphene import Schema
from backend.schema import schema



# from django.core.management import call_command

class SuperUser:
    name = 'Admin'
    is_authenticated = True

class TestContext:
    user = SuperUser()

def import_csv(file_path):
    points = pd.read_csv(file_path)
    print("CSV loaded:",points.head())

    for index, row in points.iterrows():
    # index=1
    # row={}
    # row['lon']=41.9657725845131+index
    # row['lat']=-45.14485001564026+index
        address = Address(
            addressString = 'Test address #'+str(index),
            location = geos.Point((row['lon'], row['lat'])),
        )
        address.save(omit_geocode=True)

        print("New address was created with id ",address.id)

        request = Request(
            name = 'Test request #'+str(index),
            category = Category.objects.get(pk=random.randrange(1,11,1)),
            description = 'Description for wonderful place #'+str(index),
            address = address,
            tags = ['tag1', 'tag2', 'tag3'],
            requested_by = 'test script',
        )

        request.save()
        print("New Request was created with id", request.id)

        # result = schema.execute('{ addresses { id properties {addressString} geometry {coordinates}} }')
        query = '''mutation ApproveRequest($id: ID!, $input: RequestApproveInput!) {
            approveRequest(id: $id, input: $input) {
                request {
                    id
                    name
                    category {
                        name
                    }
                    address {
                        properties {
                            addressString
                        }
                        geometry {
                            coordinates
                        }
                    }
                    description
                    dateCreated
                    dateUpdated
                    dateApproved
                    approved
                    approvedBy
                    approvedComment
                    tags
                }
            }
        }'''

        variables = {
            'id': request.id,
            'input': {
                'approvedBy': "script",
                'approvedComment': "testing backend"
            }
        }
     
        

        context = TestContext()

        print("Trying to execute [{}]".format(query))
        result = schema.execute(query,context_value=context, variables=variables)
        # result = schema.execute('{ mutation ApproveRequest { approveRequest( id: 9, input: { approvedComment: \"testing backend\", approvedBy: \"script\"}) { request { id requestedBy dateApproved dateCreated dateUpdated approvedBy approved }}}}')
        print("Graphene scheme execution:",result)
            #published_date=datetime.strptime(row['published_date'], '%Y-%m-%d').date(),
        print("====================================================")
        
    print("Done processing")
# def load_data(file_path):
#     with open(file_path, 'r', encoding='utf-8') as file:
#         text = file.read()
#     return text.lower()

# def generate_text(starting_words, model, num_words=20):
#     sentence = list(starting_words)

#     for _ in range(num_words):
#         next_word = model[tuple(sentence[-2:])].most_common(1)[0][0]
#         sentence.append(next_word)

#     return ' '.join(sentence)

if __name__ == '__main__':
    # nltk.download('punkt')
    # text_data = load_data("./../data/text/J_D_Salinger-The_Catcher_in_the_Rye-1951.txt")
    # text_data = load_data("./../data/text/pg11.txt")
    # tokens = word_tokenize(text_data)

    # trigrams = list(ngrams(tokens, 3))

    # trigram_model = defaultdict(Counter)

    # for trigram in trigrams:
    #     trigram_model[(trigram[0], trigram[1])][trigram[2]] += 1
    
    # starting_words = ("Alice", "was")
    # generated_text = generate_text(starting_words, trigram_model)
    # print(generated_text)

    csv_file_path = './../data/random-coordinates-10000.csv' 
    import_csv(csv_file_path)
