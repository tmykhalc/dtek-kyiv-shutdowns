#!/usr/bin/env python3
# FastAPI microservice wrapping your Playwright scraper

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from functools import partial
from starlette.concurrency import run_in_threadpool

# Import your refactored function from Step 1
from scrape_dtek_playwright import fetch_schedule

app = FastAPI(title="DTEK Schedule Service", version="1.0.0")

class ScheduleRequest(BaseModel):
    street: str
    house: str
    headless: Optional[bool] = True
    debug: Optional[bool] = False

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/schedule")
async def get_schedule(
    street: str = Query(..., description="Street as the site suggests it, e.g. 'вул. Перемоги'. A more specific name gives fewer suggestions"),
    house: str = Query(..., description="House number, e.g. '1'"),
    headless: bool = Query(True, description="Run Chromium headless"),
    debug: bool = Query(False, description="Enable extra diagnostics")
):
    try:
        # run blocking Playwright code in a thread
        result = await run_in_threadpool(
            partial(fetch_schedule, street=street, house=house, headless=headless, debug=debug)
        )
        return JSONResponse(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/schedule")
async def post_schedule(req: ScheduleRequest):
    try:
        result = await run_in_threadpool(
            partial(fetch_schedule, street=req.street, house=req.house,
                    headless=req.headless, debug=req.debug)
        )
        return JSONResponse(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

