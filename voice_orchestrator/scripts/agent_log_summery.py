from datetime import timedelta
import pytz
import csv
from dialer.models import Agent

from events.models import AgentLogs

PKT = pytz.timezone("Asia/Karachi")
def get_agent_summary_data(agent_id, target_date):
    logs = (
        AgentLogs.objects.filter(
            agent_id=agent_id,
            created_at__date=target_date,
            action__in=['login', 'logout']
        )
        .order_by('created_at')
    )

    if not logs.exists():
        return None

    login_times = []
    logout_times = []

    total_logged_out = timedelta()
    current_logout_time = None

    for log in logs:
        pkt_time = log.created_at.astimezone(PKT)

        if log.action == "login":
            login_times.append(pkt_time)

            if current_logout_time:
                total_logged_out += pkt_time - current_logout_time
                current_logout_time = None

        elif log.action == "logout":
            logout_times.append(pkt_time)
            current_logout_time = pkt_time

    if not login_times:
        return None

    first_login = login_times[0]
    last_logout = logout_times[-1] if logout_times else login_times[-1]

    return {
        "agent_id": agent_id,
        "first_login": first_login,
        "last_logout": last_logout,
        "total_logged_out": total_logged_out
    }


def export_team_logs_to_csv(team_name, target_date, file_name="team_report.csv"):
    agents = Agent.objects.filter(teams__name=team_name).distinct()

    rows = []

    for agent in agents:
        summary = get_agent_summary_data(agent.id, target_date)

        if not summary:
            continue

        rows.append({
            "agent_name": agent.user.get_full_name(),
            "first_login": summary["first_login"].strftime("%Y-%m-%d %I:%M:%S %p"),
            "last_logout": summary["last_logout"].strftime("%Y-%m-%d %I:%M:%S %p"),
            "total_logged_out": str(summary["total_logged_out"])
        })

    with open(file_name, "w", newline="") as csvfile:
        fieldnames = [
            "agent_name",
            "first_login",
            "last_logout",
            "total_logged_out"
        ]

        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV exported: {file_name}")


def print_agent_logs_chronological(agent_id, target_date):
    logs = (
        AgentLogs.objects.filter(
            agent_id=agent_id,
            created_at__date=target_date
        )
        .order_by('created_at')  # chronological order
    )

    if not logs.exists():
        print("No logs found")
        return

    print(f"\n=== Agent {agent_id} Logs for {target_date} (Chronological) ===\n")

    for log in logs:
        pkt_time = log.created_at.astimezone(PKT)

        print(
            f"{pkt_time.strftime('%Y-%m-%d %I:%M:%S %p')} | "
            f"{log.action} | "
            f"{log.metadata if log.metadata else '{}'}"
        )