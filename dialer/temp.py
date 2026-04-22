import csv
from django.utils import timezone
from dialer.models import CallLog

today = timezone.localdate()

call_logs = CallLog.objects.filter(
    initiated_at__date=today
).order_by('to_number', '-initiated_at').distinct('to_number')

with open('call_logs.csv', 'w') as f:
    writer = csv.writer(f)
    writer.writerow(['to_number'])  # header

    for log in call_logs:
        writer.writerow([log.to_number])