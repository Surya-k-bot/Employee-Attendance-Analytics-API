# DECISIONS.md

The unique `emp_code` index identifies employees; `(department, joined_on)` and `joined_on` support department headcount queries, while `(department, emp_code)` supports filtered employee listing. Attendance uses unique `(emp_code, date)` for the natural key and punch-in race, `(emp_code, punch_in)` for the latest punch-out candidate, `(date, emp_code)` for ordered lists, `(status, date, emp_code)` for status filters, and `(date, late_minutes, emp_code)` for monthly late scans. I rejected a standalone status index because the compound status/date index serves that filter with fewer redundant indexes and less write overhead.

Concurrent identical punch-ins both attempt insertion; the unique attendance index commits one and rejects the other. The winner returns 201 and the duplicate-key handler returns 409. The leaderboard uses `$rank`, filters `rank <= limit` after ranking, and therefore includes all employees tied at the cutoff; the final ordering breaks ties by `emp_code`.

Department summary starts from employees who joined by the period end, then looks up their logs. Thus employees with no logs remain in the headcount. At 100x the data, I would benchmark first, then move analytics reads to replicas and maintain validated daily summary data to reduce repeated scans of raw attendance history.
