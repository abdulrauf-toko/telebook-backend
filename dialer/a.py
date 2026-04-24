from django.db import migrations, models
from django.apps import apps

def clean_float_strings():
    MyModel = apps.get_model('dialer', 'Lead')
    for obj in MyModel.objects.all():
        try:
            obj.udhaar_lead_id = int(float(obj.udhaar_lead_id))  # "4645813.0" → 4645813
            obj.save()
        except (ValueError, TypeError):
            obj.udhaar_lead_id = 0  # fallback for bad data
            obj.save()