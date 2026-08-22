# Optional Phoenix example work orders

The primary CertiRoute workflow accepts a customer's uploaded CSV. This file is
an explicitly optional, one-click example for judges and first-time users; it is
not a runtime fallback or a source of temperature data.

Its six rows form a fictional Phoenix field-service shift. The landmarks are
real, but the tasks, priorities, durations, time windows, and approximate
service points are demonstration inputs—not claims about actual assets or work
at those locations. The file follows the same eight-column upload contract:

```text
job_id,name,latitude,longitude,duration_minutes,priority,earliest_start,latest_finish
```

Temperatures are not stored in this sample file. Generated or substitute
temperatures never enter heat scoring or the crew-route result. Example mode
uses real saved or newly retrieved FortyGuard temperature evidence. Completed
responses are stored locally under Git-ignored
`data/raw/fortyguard_heatmap_snapshots/`.

Do not place credentials, private customer data, or unlicensed datasets in this
directory.
