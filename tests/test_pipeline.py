#!/usr/bin/env python3
"""End-to-end test for viking_to_mfp.py.

Runs the real script against two in-process servers that replay the exact request
and response contracts the Kuchnia Vikinga panel and the MyFitnessPal web app use,
then asserts on what the script actually sent.

    python3 tests/test_pipeline.py

No network access and no accounts are needed.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT = Path(__file__).resolve().parent.parent / "viking_to_mfp.py"
RECORD: dict[str, list] = {"kv": [], "mfp": []}
# what has been "logged", served back by read_diary - mirrors MFP's item shape
DIARY: dict[str, list[dict]] = {}
FAIL_READ_DIARY = False

DELIVERIES = {
    "111": {
        "orderId": 111,
        "orderStatus": "ACTIVE",
        "deliveries": [
            {"deliveryId": 9001, "date": "2026-10-14"},
            {"deliveryId": 9002, "date": "2026-10-15"},
            {"deliveryId": 9003, "date": "2026-11-30"},
            {"deliveryId": 9004, "date": "2026-10-17"},
            {"deliveryId": 9005, "date": "2026-10-18"},
            {"deliveryId": 9006, "date": "2026-10-19"},
            {"deliveryId": 9007, "date": "2026-10-20"},
        ],
    },
    "222": {
        "orderId": 222,
        "orderStatus": "ACTIVE",
        "deliveries": [{"deliveryId": 9101, "date": "2026-10-16"}],
    },
}

FIVE_MEALS = [
    {"deliveryMealId": 1, "mealName": "\u015bniadanie", "amount": 1,
     "menuMealName": "Jajecznica",
     "nutrition": {"calories": "380", "protein": "22", "carbohydrate": "3.5", "fat": "30"}},
    {"deliveryMealId": 2, "mealName": "ii \u015bniadanie", "amount": 2,
     "menuMealName": "Twaro\u017cek",
     "nutrition": {"calories": "150", "protein": "12", "carbohydrate": "6", "fat": "8"}},
    {"deliveryMealId": 3, "mealName": "obiad", "amount": 1,
     "menuMealName": "Kurczak z ry\u017cem",
     "nutrition": {"calories": "620", "protein": "41", "carbohydrate": "70", "fat": "14"}},
    {"deliveryMealId": 4, "mealName": "podwieczorek", "amount": 1,
     "menuMealName": "Ciasto marchewkowe",
     "nutrition": {"calories": "210", "protein": "4", "carbohydrate": "30", "fat": "8"}},
    {"deliveryMealId": 5, "mealName": "kolacja", "amount": 1,
     "menuMealName": "Sa\u0142atka",
     "nutrition": {"calories": "260", "protein": "15", "carbohydrate": "12", "fat": "16"}},
]

ONE_MEAL = [{
    "deliveryMealId": 51, "mealName": "\u015bniadanie", "amount": 1,
    "menuMealName": "Owsianka z malinami",
    "nutrition": {"calories": "412.5", "protein": "18.2",
                  "carbohydrate": "55", "fat": "11.4"},
}]


def menu_for(delivery_id: str) -> dict:
    return {"deliveryMenuMeal": ONE_MEAL if delivery_id == "9101" else FIVE_MEALS}


# Real mozlz4 files: LZ4-block compressed by the reference lz4 library, exactly as
# Firefox/Zen write their session-restore store. Fixed here so the pure-Python
# decoder and the host handling can be checked with no lz4 package installed.
#
# The browser really does hold two different SESSION cookies for KV (one per
# hostname), and a stale previous.jsonlz4 alongside a fresh recovery.jsonlz4.
RECOVERY_B64 = (
    "bW96THo0MACBDwAA8Dx7ImNvb2tpZXMiOlt7Imhvc3QiOiJhcGxpa2FjamEua3VjaG5pYXZpa2lu"
    "Z2EucGwiLCJuYW1lIjoiWFNSRi1UT0tFTiIsInZhbHUVAPUwYWJjLWRlZiIsInBhdGgiOiIvIiwi"
    "ZXhwaXJ5IjowLCJzZWN1cmUiOnRydWUsImh0dHBvbmx5IjpmYWxzZX0sggD2BXd3dy5teWZpdG5l"
    "c3NwYWwuY29tewAxX19TSAD3CS1uZXh0LWF1dGguc2Vzc2lvbi10b2tlbpEAH00BACgPxgADrzE3"
    "OTQ1NDY5OTHPAAcAEAAHzgB6ZXhhbXBsZcUAV25vaXNlqgAfeAEA/30IuQIHzAFfcGFuZWwYAwlo"
    "U0VTU0lPFQNmUEFORUwtGAAPGwMbC0wCDpwDD4QAEj9BUFCCACuSXSwid2luZG93KgQTaRYBQ2ki"
    "OjEIABMyCAATMwgAEzQIABM1CAATNggAEzcIABM4CAAUOUgABVEABFIAFDFTABQxVAAUMVUAFDFW"
    "ABQxVwAUMVgAFDFZABQxWgAUMloAFTKsAARaABQyWgAUMloAFDJaABQyWgAUMloAFDJaABQyWgAU"
    "M1oAFDNaABUzBwEEWgAUM1oAFDNaABQzWgAUM1oAFDNaABQzWgAUNFoAFDRaABQ0WgAVNGIBBFoA"
    "FDRaABQ0WgAUNFoAFDRaABQ0WgAUNVoAFDVaABQ1WgAUNVoAFTW9AQRaABQ1WgAUNVoAFDVaABQ1"
    "WgAUNloAFDZaABQ2WgAUNloAFDZaABU2GAIEWgAUNloAFDZaABQ2WgAUN1oAFDdaABQ3WgAUN1oA"
    "FDdaABQ3WgAVN3MCBFoAFDdaABQ3WgAUOFoAFDhaABQ4WgAUOFoAFDhaABQ4WgAUOFoAFTjOAgRa"
    "ABQ4WgAUOVoAFDlaABQ5WgAUOVoAFDlaABQ5WgAUOVoAFDlaABU5KQMGKgMFKwMVMCwDFTAtAxUw"
    "LgMVMC8DFTAwAxUwMQMVMDIDFTAzAxUwZAAHjwMFZAAGkQMGkgMGkwMGlAMGlQMGlgMGlwMFmAMV"
    "MZkDJjEy9AMFZAAFnAMVMZ0DFTGeAxUxnwMVMaADFTGhAxUxogMVMaMDFTGkAyYxM1kEBWQABacD"
    "FTGoAxUxqQMVMaoDFTGrAxUxrAMVMa0DFTGuAxUxrwMmMTS+BAVkAAWyAxUxswMVMbQDFTG1AxUx"
    "tgMVMbcDFTG4AxUxuQMVMboDJjE1IwUFZAAFvQMVMb4DFTG/AxUxwAMVMcEDFTHCAxUxwwMVMcQD"
    "FTHFAyYxNogFBWQABcgDFTHJAxUxygMVMcsDFTHMAxUxzQMVMc4DFTHPAxUx0AMmMTftBQVkAAXT"
    "AxUx1AMVMdUDFTHWAxUx1wMVMdgDFTHZAxUx2gMVMdsDJjE4UgYFZAAF3gMVMd8DFTHgAxUx4QMV"
    "MeIDFTHjAxUx5AMVMeUDFTHmAyYxObcGBrgGBbkGBegDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugD"
    "FTLoAxUy6AMmMjEdBwVkAAXoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAxYygAcGgQcHggcFZAAG"
    "hAcGhQcGhgcGhwcGiAcGiQcF6AMVMugDFTLoAyYyM+cHBWQABegDFTLoAxUy6AMVMugDFTLoAxUy"
    "6AMVMugDFTLoAxUy6AMmMjRMCAVkAAXoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugD"
    "JjI1sQgFZAAF6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAyYyNhYJBWQABegDFTLo"
    "AxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMmMjd7CQVkAAXoAxUy6AMVMugDFTLoAxUy6AMV"
    "MugDFTLoAxUy6AMVMugDJjI44AkFZAAF6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLo"
    "AxUy6ANgMjk5fV19"
)
PREVIOUS_B64 = (
    "bW96THo0MAADDwAA8Dx7ImNvb2tpZXMiOlt7Imhvc3QiOiJhcGxpa2FjamEua3VjaG5pYXZpa2lu"
    "Z2EucGwiLCJuYW1lIjoiWFNSRi1UT0tFTiIsInZhbHUVAPUwYWJjLWRlZiIsInBhdGgiOiIvIiwi"
    "ZXhwaXJ5IjowLCJzZWN1cmUiOnRydWUsImh0dHBvbmx5IjpmYWxzZX0sggD2BXd3dy5teWZpdG5l"
    "c3NwYWwuY29tewAxX19TSAD3CS1uZXh0LWF1dGguc2Vzc2lvbi10b2tlbpEAH00BACgPxgADrzE3"
    "OTQ1NDY5OTHPAAcAEAAHzgB6ZXhhbXBsZcUAV25vaXNlqgAfeAEA/30IuQIvfSwcAxtoU0VTU0lP"
    "GQNmU1RBTEUtGAAPHwMbAVACkl0sIndpbmRvd6wDE2mYAENpIjoxCAATMggAEzMIABM0CAATNQgA"
    "EzYIABM3CAATOAgAFDlIAAVRAARSABQxUwAUMVQAFDFVABQxVgAUMVcAFDFYABQxWQAUMVoAFDJa"
    "ABUyrAAEWgAUMloAFDJaABQyWgAUMloAFDJaABQyWgAUMloAFDNaABQzWgAVMwcBBFoAFDNaABQz"
    "WgAUM1oAFDNaABQzWgAUM1oAFDRaABQ0WgAUNFoAFTRiAQRaABQ0WgAUNFoAFDRaABQ0WgAUNFoA"
    "FDVaABQ1WgAUNVoAFDVaABU1vQEEWgAUNVoAFDVaABQ1WgAUNVoAFDZaABQ2WgAUNloAFDZaABQ2"
    "WgAVNhgCBFoAFDZaABQ2WgAUNloAFDdaABQ3WgAUN1oAFDdaABQ3WgAUN1oAFTdzAgRaABQ3WgAU"
    "N1oAFDhaABQ4WgAUOFoAFDhaABQ4WgAUOFoAFDhaABU4zgIEWgAUOFoAFDlaABQ5WgAUOVoAFDla"
    "ABQ5WgAUOVoAFDlaABQ5WgAVOSkDBioDBSsDFTAsAxUwLQMVMC4DFTAvAxUwMAMVMDEDFTAyAxUw"
    "MwMVMGQAB48DBWQABpEDBpIDBpMDBpQDBpUDBpYDBpcDBZgDFTGZAyYxMvQDBWQABZwDFTGdAxUx"
    "ngMVMZ8DFTGgAxUxoQMVMaIDFTGjAxUxpAMmMTNZBAVkAAWnAxUxqAMVMakDFTGqAxUxqwMVMawD"
    "FTGtAxUxrgMVMa8DJjE0vgQFZAAFsgMVMbMDFTG0AxUxtQMVMbYDFTG3AxUxuAMVMbkDFTG6AyYx"
    "NSMFBWQABb0DFTG+AxUxvwMVMcADFTHBAxUxwgMVMcMDFTHEAxUxxQMmMTaIBQVkAAXIAxUxyQMV"
    "McoDFTHLAxUxzAMVMc0DFTHOAxUxzwMVMdADJjE37QUFZAAF0wMVMdQDFTHVAxUx1gMVMdcDFTHY"
    "AxUx2QMVMdoDFTHbAyYxOFIGBWQABd4DFTHfAxUx4AMVMeEDFTHiAxUx4wMVMeQDFTHlAxUx5gMm"
    "MTm3Bga4BgW5BgXoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDJjIxHQcFZAAF6AMV"
    "MugDFTLoAxUy6AMVMugDFTLoAxUy6AMWMoAHBoEHB4IHBWQABoQHBoUHBoYHBocHBogHBokHBegD"
    "FTLoAxUy6AMmMjPnBwVkAAXoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDJjI0TAgF"
    "ZAAF6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAyYyNbEIBWQABegDFTLoAxUy6AMV"
    "MugDFTLoAxUy6AMVMugDFTLoAxUy6AMmMjYWCQVkAAXoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLo"
    "AxUy6AMVMugDJjI3ewkFZAAF6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAyYyOOAJ"
    "BWQABegDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDFTLoAxUy6AMVMugDYDI5OX1dfQ=="
)
RECOVERY_PLAIN_B64 = (
    "eyJjb29raWVzIjpbeyJob3N0IjoiYXBsaWthY2phLmt1Y2huaWF2aWtpbmdhLnBsIiwibmFtZSI6"
    "IlhTUkYtVE9LRU4iLCJ2YWx1ZSI6ImFiYy1kZWYiLCJwYXRoIjoiLyIsImV4cGlyeSI6MCwic2Vj"
    "dXJlIjp0cnVlLCJodHRwb25seSI6ZmFsc2V9LHsiaG9zdCI6Ind3dy5teWZpdG5lc3NwYWwuY29t"
    "IiwibmFtZSI6Il9fU2VjdXJlLW5leHQtYXV0aC5zZXNzaW9uLXRva2VuIiwidmFsdWUiOiJNTU1N"
    "TU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU0i"
    "LCJwYXRoIjoiLyIsImV4cGlyeSI6MTc5NDU0Njk5MSwic2VjdXJlIjp0cnVlLCJodHRwb25seSI6"
    "dHJ1ZX0seyJob3N0IjoiZXhhbXBsZS5jb20iLCJuYW1lIjoibm9pc2UiLCJ2YWx1ZSI6Inh4eHh4"
    "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
    "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
    "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
    "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
    "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
    "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
    "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHgiLCJl"
    "eHBpcnkiOjB9LHsiaG9zdCI6InBhbmVsLmt1Y2huaWF2aWtpbmdhLnBsIiwibmFtZSI6IlNFU1NJ"
    "T04iLCJ2YWx1ZSI6IlBBTkVMLVNFU1NJT04iLCJwYXRoIjoiLyIsImV4cGlyeSI6MCwic2VjdXJl"
    "Ijp0cnVlLCJodHRwb25seSI6dHJ1ZX0seyJob3N0IjoiYXBsaWthY2phLmt1Y2huaWF2aWtpbmdh"
    "LnBsIiwibmFtZSI6IlNFU1NJT04iLCJ2YWx1ZSI6IkFQUC1TRVNTSU9OIiwicGF0aCI6Ii8iLCJl"
    "eHBpcnkiOjAsInNlY3VyZSI6dHJ1ZSwiaHR0cG9ubHkiOnRydWV9XSwid2luZG93cyI6W3siaSI6"
    "MH0seyJpIjoxfSx7ImkiOjJ9LHsiaSI6M30seyJpIjo0fSx7ImkiOjV9LHsiaSI6Nn0seyJpIjo3"
    "fSx7ImkiOjh9LHsiaSI6OX0seyJpIjoxMH0seyJpIjoxMX0seyJpIjoxMn0seyJpIjoxM30seyJp"
    "IjoxNH0seyJpIjoxNX0seyJpIjoxNn0seyJpIjoxN30seyJpIjoxOH0seyJpIjoxOX0seyJpIjoy"
    "MH0seyJpIjoyMX0seyJpIjoyMn0seyJpIjoyM30seyJpIjoyNH0seyJpIjoyNX0seyJpIjoyNn0s"
    "eyJpIjoyN30seyJpIjoyOH0seyJpIjoyOX0seyJpIjozMH0seyJpIjozMX0seyJpIjozMn0seyJp"
    "IjozM30seyJpIjozNH0seyJpIjozNX0seyJpIjozNn0seyJpIjozN30seyJpIjozOH0seyJpIjoz"
    "OX0seyJpIjo0MH0seyJpIjo0MX0seyJpIjo0Mn0seyJpIjo0M30seyJpIjo0NH0seyJpIjo0NX0s"
    "eyJpIjo0Nn0seyJpIjo0N30seyJpIjo0OH0seyJpIjo0OX0seyJpIjo1MH0seyJpIjo1MX0seyJp"
    "Ijo1Mn0seyJpIjo1M30seyJpIjo1NH0seyJpIjo1NX0seyJpIjo1Nn0seyJpIjo1N30seyJpIjo1"
    "OH0seyJpIjo1OX0seyJpIjo2MH0seyJpIjo2MX0seyJpIjo2Mn0seyJpIjo2M30seyJpIjo2NH0s"
    "eyJpIjo2NX0seyJpIjo2Nn0seyJpIjo2N30seyJpIjo2OH0seyJpIjo2OX0seyJpIjo3MH0seyJp"
    "Ijo3MX0seyJpIjo3Mn0seyJpIjo3M30seyJpIjo3NH0seyJpIjo3NX0seyJpIjo3Nn0seyJpIjo3"
    "N30seyJpIjo3OH0seyJpIjo3OX0seyJpIjo4MH0seyJpIjo4MX0seyJpIjo4Mn0seyJpIjo4M30s"
    "eyJpIjo4NH0seyJpIjo4NX0seyJpIjo4Nn0seyJpIjo4N30seyJpIjo4OH0seyJpIjo4OX0seyJp"
    "Ijo5MH0seyJpIjo5MX0seyJpIjo5Mn0seyJpIjo5M30seyJpIjo5NH0seyJpIjo5NX0seyJpIjo5"
    "Nn0seyJpIjo5N30seyJpIjo5OH0seyJpIjo5OX0seyJpIjoxMDB9LHsiaSI6MTAxfSx7ImkiOjEw"
    "Mn0seyJpIjoxMDN9LHsiaSI6MTA0fSx7ImkiOjEwNX0seyJpIjoxMDZ9LHsiaSI6MTA3fSx7Imki"
    "OjEwOH0seyJpIjoxMDl9LHsiaSI6MTEwfSx7ImkiOjExMX0seyJpIjoxMTJ9LHsiaSI6MTEzfSx7"
    "ImkiOjExNH0seyJpIjoxMTV9LHsiaSI6MTE2fSx7ImkiOjExN30seyJpIjoxMTh9LHsiaSI6MTE5"
    "fSx7ImkiOjEyMH0seyJpIjoxMjF9LHsiaSI6MTIyfSx7ImkiOjEyM30seyJpIjoxMjR9LHsiaSI6"
    "MTI1fSx7ImkiOjEyNn0seyJpIjoxMjd9LHsiaSI6MTI4fSx7ImkiOjEyOX0seyJpIjoxMzB9LHsi"
    "aSI6MTMxfSx7ImkiOjEzMn0seyJpIjoxMzN9LHsiaSI6MTM0fSx7ImkiOjEzNX0seyJpIjoxMzZ9"
    "LHsiaSI6MTM3fSx7ImkiOjEzOH0seyJpIjoxMzl9LHsiaSI6MTQwfSx7ImkiOjE0MX0seyJpIjox"
    "NDJ9LHsiaSI6MTQzfSx7ImkiOjE0NH0seyJpIjoxNDV9LHsiaSI6MTQ2fSx7ImkiOjE0N30seyJp"
    "IjoxNDh9LHsiaSI6MTQ5fSx7ImkiOjE1MH0seyJpIjoxNTF9LHsiaSI6MTUyfSx7ImkiOjE1M30s"
    "eyJpIjoxNTR9LHsiaSI6MTU1fSx7ImkiOjE1Nn0seyJpIjoxNTd9LHsiaSI6MTU4fSx7ImkiOjE1"
    "OX0seyJpIjoxNjB9LHsiaSI6MTYxfSx7ImkiOjE2Mn0seyJpIjoxNjN9LHsiaSI6MTY0fSx7Imki"
    "OjE2NX0seyJpIjoxNjZ9LHsiaSI6MTY3fSx7ImkiOjE2OH0seyJpIjoxNjl9LHsiaSI6MTcwfSx7"
    "ImkiOjE3MX0seyJpIjoxNzJ9LHsiaSI6MTczfSx7ImkiOjE3NH0seyJpIjoxNzV9LHsiaSI6MTc2"
    "fSx7ImkiOjE3N30seyJpIjoxNzh9LHsiaSI6MTc5fSx7ImkiOjE4MH0seyJpIjoxODF9LHsiaSI6"
    "MTgyfSx7ImkiOjE4M30seyJpIjoxODR9LHsiaSI6MTg1fSx7ImkiOjE4Nn0seyJpIjoxODd9LHsi"
    "aSI6MTg4fSx7ImkiOjE4OX0seyJpIjoxOTB9LHsiaSI6MTkxfSx7ImkiOjE5Mn0seyJpIjoxOTN9"
    "LHsiaSI6MTk0fSx7ImkiOjE5NX0seyJpIjoxOTZ9LHsiaSI6MTk3fSx7ImkiOjE5OH0seyJpIjox"
    "OTl9LHsiaSI6MjAwfSx7ImkiOjIwMX0seyJpIjoyMDJ9LHsiaSI6MjAzfSx7ImkiOjIwNH0seyJp"
    "IjoyMDV9LHsiaSI6MjA2fSx7ImkiOjIwN30seyJpIjoyMDh9LHsiaSI6MjA5fSx7ImkiOjIxMH0s"
    "eyJpIjoyMTF9LHsiaSI6MjEyfSx7ImkiOjIxM30seyJpIjoyMTR9LHsiaSI6MjE1fSx7ImkiOjIx"
    "Nn0seyJpIjoyMTd9LHsiaSI6MjE4fSx7ImkiOjIxOX0seyJpIjoyMjB9LHsiaSI6MjIxfSx7Imki"
    "OjIyMn0seyJpIjoyMjN9LHsiaSI6MjI0fSx7ImkiOjIyNX0seyJpIjoyMjZ9LHsiaSI6MjI3fSx7"
    "ImkiOjIyOH0seyJpIjoyMjl9LHsiaSI6MjMwfSx7ImkiOjIzMX0seyJpIjoyMzJ9LHsiaSI6MjMz"
    "fSx7ImkiOjIzNH0seyJpIjoyMzV9LHsiaSI6MjM2fSx7ImkiOjIzN30seyJpIjoyMzh9LHsiaSI6"
    "MjM5fSx7ImkiOjI0MH0seyJpIjoyNDF9LHsiaSI6MjQyfSx7ImkiOjI0M30seyJpIjoyNDR9LHsi"
    "aSI6MjQ1fSx7ImkiOjI0Nn0seyJpIjoyNDd9LHsiaSI6MjQ4fSx7ImkiOjI0OX0seyJpIjoyNTB9"
    "LHsiaSI6MjUxfSx7ImkiOjI1Mn0seyJpIjoyNTN9LHsiaSI6MjU0fSx7ImkiOjI1NX0seyJpIjoy"
    "NTZ9LHsiaSI6MjU3fSx7ImkiOjI1OH0seyJpIjoyNTl9LHsiaSI6MjYwfSx7ImkiOjI2MX0seyJp"
    "IjoyNjJ9LHsiaSI6MjYzfSx7ImkiOjI2NH0seyJpIjoyNjV9LHsiaSI6MjY2fSx7ImkiOjI2N30s"
    "eyJpIjoyNjh9LHsiaSI6MjY5fSx7ImkiOjI3MH0seyJpIjoyNzF9LHsiaSI6MjcyfSx7ImkiOjI3"
    "M30seyJpIjoyNzR9LHsiaSI6Mjc1fSx7ImkiOjI3Nn0seyJpIjoyNzd9LHsiaSI6Mjc4fSx7Imki"
    "OjI3OX0seyJpIjoyODB9LHsiaSI6MjgxfSx7ImkiOjI4Mn0seyJpIjoyODN9LHsiaSI6Mjg0fSx7"
    "ImkiOjI4NX0seyJpIjoyODZ9LHsiaSI6Mjg3fSx7ImkiOjI4OH0seyJpIjoyODl9LHsiaSI6Mjkw"
    "fSx7ImkiOjI5MX0seyJpIjoyOTJ9LHsiaSI6MjkzfSx7ImkiOjI5NH0seyJpIjoyOTV9LHsiaSI6"
    "Mjk2fSx7ImkiOjI5N30seyJpIjoyOTh9LHsiaSI6Mjk5fV19"
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output readable
        pass

    def _send(self, code: int, payload, set_cookie: str | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length).decode() if length else ""

    def do_GET(self):
        url = urlparse(self.path)
        site = "kv" if url.path.startswith("/api/company") else "mfp"
        RECORD[site].append(
            ("GET", url.path, self.headers.get("Cookie"),
             self.headers.get("X-Launcher-Type"), url.query)
        )
        if url.path == "/api/company/customer/client":
            if "SESSION=abc" not in (self.headers.get("Cookie") or ""):
                return self._send(403, {"status": 403, "error": "Forbidden"})
            return self._send(200, {"email": "smoke@example.invalid", "clientId": 7},
                              set_cookie="XSRF-TOKEN=rotated; Path=/; Secure")
        if url.path == "/api/company/customer/order/all":
            return self._send(200, [{"orderId": 111}, {"orderId": 222}])
        if url.path.startswith("/api/company/customer/order/"):
            return self._send(200, DELIVERIES.get(url.path.rsplit("/", 1)[-1], {}))
        if url.path.startswith("/api/company/general/menus/delivery/"):
            return self._send(200, menu_for(url.path.split("/")[6]),
                              set_cookie="__cf_bm=rotated; Path=/; Secure")
        if url.path == "/api/auth/csrf":
            return self._send(200, {"csrfToken": "csrf-smoke-token"},
                              set_cookie="__Host-next-auth.csrf-token=smoke; Path=/; Secure")
        if url.path == "/api/services/diary/read_diary":
            if FAIL_READ_DIARY:
                return self._send(500, {"error": "diary unavailable"})
            query = parse_qs(url.query)
            day = (query.get("entry_date") or [""])[0]
            return self._send(200, DIARY.get(day, []))
        if url.path == "/api/services/users":
            query = parse_qs(url.query)
            assert any("diary_preferences" in v for v in query.get("fields[]", [])), url.query
            return self._send(200, {
                "diary_preferences": {
                    "meal_names": ["Breakfast", "2nd Breakfast", "Lunch", "Dessert", "Supper"]
                },
                "username": "smokeuser",
                "account": {"username": "smokeuser"},
            })
        return self._send(404, {"error": "not found", "path": url.path})

    def do_POST(self):
        body = self._read()
        RECORD["mfp"].append(("POST", self.path, body,
                              self.headers.get("X-CSRF-Token"),
                              self.headers.get("Cookie")))
        if self.path == "/api/services/diary":
            items = json.loads(body)["items"]
            for n, item in enumerate(items):
                DIARY.setdefault(item["date"], []).append({
                    "id": f"entry-{len(DIARY.get(item['date'], []))}-{n}",
                    "type": "food_entry",
                    "date": item["date"],
                    "meal_name": item["meal_name"],
                    "nutritional_contents": item["nutritional_contents"],
                })
            return self._send(200, {"items": items})
        return self._send(404, {"error": "not found"})


def start_server() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return f"--kv-base http://127.0.0.1:{port}/api --mfp-base http://127.0.0.1:{port}"


KV_COOKIE = "kv=SESSION=abc; remember-me=def"
MFP_COOKIE = "mfp=__Secure-next-auth.session-token=xyz"


def run(*extra: str, expect: int = 0) -> str:
    cmd = [sys.executable, str(SCRIPT), "--cookie", KV_COOKIE, "--cookie", MFP_COOKIE]
    cmd += list(extra)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != expect:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise AssertionError(f"exit {proc.returncode} != {expect} for {' '.join(extra)}")
    return proc.stdout


def posted_items() -> list[dict]:
    return [json.loads(r[2])["items"][0] for r in RECORD["mfp"] if r[0] == "POST"]


def main() -> int:
    base = start_server().split()
    failures = []

    def check(name: str, fn) -> None:
        # each check starts from an empty diary; RECORD["kv"] deliberately keeps
        # accumulating because one check asserts on the whole KV call history
        DIARY.clear()
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
            print(f"FAIL {name}: {exc}")
        else:
            print(f"ok   {name}")

    def dry_run_writes_nothing():
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-16", "--dry-run")
        assert not posted_items(), "dry run posted to the diary"

    def range_is_respected():
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-16", "--yes")
        items = posted_items()
        assert len(items) == 11, f"expected 5+5+1 meals, got {len(items)}"
        assert all(i["date"] != "2026-11-30" for i in items), "out-of-range delivery leaked"
        assert {i["date"] for i in items} == {"2026-10-14", "2026-10-15", "2026-10-16"}

    def payload_matches_mfp_quick_add():
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-14", "--yes")
        items = posted_items()
        assert len(items) == 5, items
        first = items[0]
        assert first == {
            "type": "quick_add",
            "date": "2026-10-14",
            "meal_name": "Breakfast",
            "nutritional_contents": {
                "fat": 30.0, "carbohydrates": 3.5, "protein": 22.0,
                "energy": {"value": 380.0, "unit": "calories"},
            },
        }, first
        # KV sends amount=2 for the second breakfast: macros must be doubled
        second = [i for i in items if i["meal_name"] == "2nd Breakfast"]
        assert len(second) == 1
        assert second[0]["nutritional_contents"] == {
            "fat": 16.0, "carbohydrates": 12.0, "protein": 24.0,
            "energy": {"value": 300.0, "unit": "calories"},
        }, second
        assert all(isinstance(i["nutritional_contents"]["fat"], float) for i in items)
        assert all(r[3] == "csrf-smoke-token" for r in RECORD["mfp"] if r[0] == "POST")

    def slot_mapping_covers_all_kv_meals():
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-14", "--yes")
        assert {i["meal_name"] for i in posted_items()} == {
            "Breakfast", "2nd Breakfast", "Lunch", "Dessert", "Supper"
        }

    def explicit_meal_map_wins():
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-14", "--yes",
            "--meal-map", "\u015bniadanie=Brunch")
        assert "Brunch" in {i["meal_name"] for i in posted_items()}

    def from_only_runs_to_last_delivery():
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-16", "--yes")
        assert {i["date"] for i in posted_items()} == {
            "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19",
            "2026-10-20", "2026-11-30",
        }

    def kv_is_never_written_to():
        assert RECORD["kv"], "no Kuchnia Vikinga calls recorded"
        assert all(r[0] == "GET" for r in RECORD["kv"]), "a non-GET reached Kuchnia Vikinga"
        assert all(r[3] == "BROWSER_PANEL" for r in RECORD["kv"])
        assert all("remember-me" in (r[2] or "") for r in RECORD["kv"]), "cookies not forwarded"
        source = SCRIPT.read_text(encoding="utf-8")
        body = source[source.index("class KVClient"):source.index("class MFPClient")]
        for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete",
                     ".post(", ".put(", ".patch(", ".delete("):
            assert verb not in body, f"KVClient must never issue {verb}"

    def mfp_writes_are_allow_listed():
        sys.path.insert(0, str(SCRIPT.parent))
        import viking_to_mfp as v

        client = v.MFPClient([v.CookieRecord("x", "y")], base="http://127.0.0.1:1")
        for method, path in (("DELETE", "/api/services/diary/1"),
                             ("PUT", "/api/services/diary/1"),
                             ("PATCH", "/api/services/diary/1"),
                             ("POST", "/api/services/users"),
                             ("POST", "/api/services/diary/read_diary"),
                             ("GET", "/api/services/diary/1"),
                             ("GET", "/api/services/diary")):
            try:
                client._request(method, path)
            except PermissionError:
                continue
            raise AssertionError(f"{method} {path} was not refused")
        assert not [r for r in RECORD["mfp"] if r[0] in ("DELETE", "PUT", "PATCH")]

    def already_logged_meals_are_skipped():
        # whatever a previous run wrote is visible in the diary, so a second run
        # must add nothing - with no local bookkeeping to go stale
        DIARY.clear()
        RECORD["mfp"].clear()
        first = run(*base, "--from", "2026-10-14", "--to", "2026-10-16", "--yes")
        assert len(posted_items()) == 11, len(posted_items())

        RECORD["mfp"].clear()
        second = run(*base, "--from", "2026-10-14", "--to", "2026-10-16", "--yes")
        assert posted_items() == [], "a second run duplicated entries"
        assert "already in the diary" in second, second

        # a wider range only fills the gaps
        RECORD["mfp"].clear()
        third = run(*base, "--from", "2026-10-14", "--to", "2026-10-20", "--yes")
        added = sorted((i["date"], i["meal_name"]) for i in posted_items())
        assert len(added) == 20, added
        assert {d for d, _ in added} == {
            "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20",
        }, added
        # the three days already logged are untouched, including 2026-10-16
        assert ("2026-10-16", "Breakfast") not in added, added
        assert "already in the diary, skipped: 11 meal(s)" in third, third

        # --force overrides it
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-16", "--yes", "--force")
        assert len(posted_items()) == 11, len(posted_items())

    def an_unreadable_diary_refuses_to_write():
        global FAIL_READ_DIARY
        DIARY.clear()
        RECORD["mfp"].clear()
        FAIL_READ_DIARY = True
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--cookie", KV_COOKIE, "--cookie", MFP_COOKIE,
                 "--from", "2026-10-14", "--to", "2026-10-14", "--yes", *base],
                capture_output=True, text=True)
            out = proc.stdout + proc.stderr
            assert proc.returncode != 0, out
            assert "refusing to add entries that may already be there" in out, out
            assert not posted_items(), "wrote without being able to check the diary"
        finally:
            FAIL_READ_DIARY = False

    def dry_run_reports_what_it_would_skip():
        DIARY.clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-14", "--yes")
        RECORD["mfp"].clear()
        out = run(*base, "--from", "2026-10-14", "--to", "2026-10-14", "--dry-run")
        assert "already in the diary" in out, out
        assert "nothing to copy" not in out
        assert not posted_items(), "dry run must not write"

    def dead_session_fails_loudly():
        RECORD["mfp"].clear()
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--cookie", "kv=SESSION=bogus",
             "--cookie", MFP_COOKIE, "--from", "2026-10-14", "--yes", *base],
            capture_output=True, text=True)
        assert proc.returncode != 0, "a dead Kuchnia Vikinga session must fail"
        assert "not valid" in proc.stdout + proc.stderr, proc.stdout + proc.stderr
        assert not posted_items(), "must not write with a dead Kuchnia Vikinga session"

    def harvested_cookies_survive_set_cookie():
        RECORD["kv"].clear()
        RECORD["mfp"].clear()
        run(*base, "--from", "2026-10-14", "--to", "2026-10-14", "--yes")
        assert RECORD["kv"] and RECORD["mfp"]
        for r in RECORD["kv"]:
            assert "remember-me=def" in (r[2] or ""), f"KV session cookie lost: {r}"
        for r in RECORD["mfp"]:
            cookie = r[4] if r[0] == "POST" else r[2]
            assert "__Secure-next-auth.session-token=xyz" in (cookie or ""), \
                f"MFP session cookie lost: {r}"

    check("dry run writes nothing", dry_run_writes_nothing)
    check("date range is respected", range_is_respected)
    check("payload matches the MFP quick-add contract", payload_matches_mfp_quick_add)
    check("every KV meal slot is mapped", slot_mapping_covers_all_kv_meals)
    check("--meal-map overrides the mapping", explicit_meal_map_wins)
    check("--from alone runs to the last delivery", from_only_runs_to_last_delivery)
    check("Kuchnia Vikinga is only ever read", kv_is_never_written_to)
    check("MFP writes are allow-listed", mfp_writes_are_allow_listed)
    check("meals already in the diary are skipped", already_logged_meals_are_skipped)
    check("an unreadable diary refuses to write", an_unreadable_diary_refuses_to_write)
    check("dry run reports what it would skip", dry_run_reports_what_it_would_skip)
    check("dead session fails loudly", dead_session_fails_loudly)
    check("harvested cookies survive Set-Cookie responses",
          harvested_cookies_survive_set_cookie)

    def zen_session_cookies_are_readable():
        import base64
        import pathlib
        import tempfile

        from requests.cookies import get_cookie_header

        sys.path.insert(0, str(SCRIPT.parent))
        import viking_to_mfp as v

        recovery = base64.b64decode(RECOVERY_B64)
        previous = base64.b64decode(PREVIOUS_B64)
        plain = base64.b64decode(RECOVERY_PLAIN_B64)

        # pure-python LZ4 block decoder, no lz4 package needed
        assert v._lz4_block_decompress(recovery[12:]) == plain

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="kv2mfp-ff-"))
        profile = tmp / "qvtj30fw.Default Profile"
        (profile / "sessionstore-backups").mkdir(parents=True)
        (profile / "sessionstore-backups" / "recovery.jsonlz4").write_bytes(recovery)
        (profile / "sessionstore-backups" / "previous.jsonlz4").write_bytes(previous)
        original = v.FIREFOX_ROOTS
        v.FIREFOX_ROOTS = (str(tmp),)
        try:
            records = v.harvest_firefox_sessionstore(("kuchniavikinga.pl", "myfitnesspal.com"))
            seen = {(r.host, r.name): r.value for r in records}
            assert seen[("aplikacja.kuchniavikinga.pl", "SESSION")] == "APP-SESSION", seen
            assert seen[("panel.kuchniavikinga.pl", "SESSION")] == "PANEL-SESSION", seen
            assert seen[("www.myfitnesspal.com", "__Secure-next-auth.session-token")] \
                == "M" * 60
            assert not any("example.com" in r.host for r in records), "leaked other sites"

            def sent(host):
                jar = v.cookie_jar(records, host)
                return get_cookie_header(jar, requests.Request("GET", f"https://{host}/").prepare()) or ""

            # the crux: each SESSION must go to its own host and never to the other
            app = sent("aplikacja.kuchniavikinga.pl")
            assert "SESSION=APP-SESSION" in app, app
            assert "PANEL-SESSION" not in app, app
            panel = sent("panel.kuchniavikinga.pl")
            assert "SESSION=PANEL-SESSION" in panel, panel
            assert "APP-SESSION" not in panel, panel
            assert "XSRF-TOKEN=abc-def" in app, app
            assert "__Secure-next-auth.session-token=" in sent("www.myfitnesspal.com")
            # a host-only cookie must not be sent to an unrelated host
            assert sent("evil.example.com") == ""

            # store precedence: the persistent copy wins over a later duplicate
            shadowed = [v.CookieRecord("SESSION", "FROM-COOKIE-DB", "panel.kuchniavikinga.pl")] \
                + records
            jar = v.cookie_jar(shadowed, "panel.kuchniavikinga.pl")
            header = get_cookie_header(
                jar, requests.Request("GET", "https://panel.kuchniavikinga.pl/").prepare()) or ""
            assert "SESSION=FROM-COOKIE-DB" in header, header
        finally:
            v.FIREFOX_ROOTS = original

    check("Zen session-store cookies are readable per host",
          zen_session_cookies_are_readable)

    def login_is_detected_per_site():
        import argparse
        import pathlib
        import tempfile

        sys.path.insert(0, str(SCRIPT.parent))
        import viking_to_mfp as v

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="kv2mfp-sess-"))
        kv_cache = tmp / "kv.json"
        mfp_cache = tmp / "mfp.json"
        v.save_cached_cookies(mfp_cache, [v.CookieRecord(
            "__Secure-next-auth.session-token", "good", "www.myfitnesspal.com")])

        kv_recipe = v.SessionRecipe("kv", "https://panel.kuchniavikinga.pl/",
                                    ("kuchniavikinga.pl",), kv_cache, "KV_COOKIE", "panel login")
        mfp_recipe = v.SessionRecipe("mfp", "https://www.myfitnesspal.com/account/login",
                                     ("myfitnesspal.com",), mfp_cache, "MFP_COOKIE", "mfp login")
        args = argparse.Namespace(cookie={}, relogin=False, login_timeout=0, login_poll=0)

        opened = []
        real_open, real_harvest, real_dump = (
            v.webbrowser.open, v.harvest_cookies, v.dump_cookies)
        v.webbrowser.open = lambda url: (opened.append(url), True)[1]
        v.dump_cookies = lambda *a, **k: None
        try:
            # 1. nothing cached, but the browser is already logged in: adopt it
            #    silently instead of opening a tab the user does not need
            v.harvest_cookies = lambda *a, **k: [
                v.CookieRecord("SESSION", "live", "panel.kuchniavikinga.pl")]
            session = v.ensure_session(kv_recipe, args, lambda c: None, "KV")
            assert session.source == "browser", session.source
            assert opened == [], f"opened a login tab unnecessarily: {opened}"

            # 2. a stale cached session must be detected and refreshed
            v.save_cached_cookies(kv_cache,
                                  [v.CookieRecord("SESSION", "stale", "panel.kuchniavikinga.pl")])
            opened.clear()
            v.harvest_cookies = lambda *a, **k: []
            try:
                v.ensure_session(kv_recipe, args, lambda c: "not valid", "KV")
            except SystemExit:
                pass
            else:
                raise AssertionError("a stale session must not be accepted")
            assert opened == ["https://panel.kuchniavikinga.pl/"], opened

            # 3. ...but a stale Kuchnia Vikinga session must not drag MyFitnessPal
            #    back through a login it does not need
            opened.clear()
            session = v.ensure_session(mfp_recipe, args, lambda c: None, "MFP")
            assert session.source.startswith("cache"), session.source
            assert opened == [], f"MFP sent back to login needlessly: {opened}"

            # 4. nothing cached and no browser session -> MFP must ask for a login
            opened.clear()
            mfp_cache.unlink()
            try:
                v.ensure_session(mfp_recipe, args, lambda c: "not valid", "MFP")
            except SystemExit:
                pass
            assert opened == ["https://www.myfitnesspal.com/account/login"], opened
        finally:
            v.webbrowser.open, v.harvest_cookies, v.dump_cookies = (
                real_open, real_harvest, real_dump)

    check("login need is detected per site", login_is_detected_per_site)

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
