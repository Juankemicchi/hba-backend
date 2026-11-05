from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from nba_module import analyze_prop
from pydantic import BaseModel

app = FastAPI(title="HBA NBA Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PropRequest(BaseModel):
    player: str
    prop: str
    line: float
    last_n: int = 12

@app.get("/")
def root():
    return {"message": "HBA NBA Backend activo"}

@app.post("/analyze")
def analyze(request: PropRequest):
    try:
        df, stats = analyze_prop(
            player_name=request.player,
            prop=request.prop,
            line=request.line,
            last_n=request.last_n
        )
        
        games = df[['GAME_DATE', 'MATCHUP', stats['value_col'], 'Over']].to_dict(orient='records')
        
        return {
            "stats": stats,
            "games": games
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))