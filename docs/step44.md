# Step 44 controlled video production

This workflow sits on top of PR 43. It does not replace Grok video jobs, media rendering, production state, quality binding, review, or export.

Flow: approved story production -> four explicit Grok jobs -> saved status -> controlled download -> media manifest -> local preview -> quality report -> review decision -> export-preview.

Each paid submit needs `--consent paid-generate:JOB_ID`. An uncertain submission is stored and is not retried unless resume is explicit. Demo mode uses no network and blocks export.

```powershell
python scripts/step44_demo.py
.\.venv\Scripts\python.exe -m vicekrack video-production-demo
```

Generated footage stays illustrative, rights-unverified, and publishable false until the existing approval gates pass.
