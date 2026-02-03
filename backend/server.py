from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import httpx
import random
import string

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT Secret
JWT_SECRET = os.environ.get('JWT_SECRET', 'nammayatra_jwt_secret_2025')
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY', '')

# Create the main app
app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========================= FARE CONFIGURATION =========================
BASE_FARE = 30  # ₹30 base fare
PER_KM_RATE = 15  # ₹15 per km
FARE_FLEXIBILITY = 0.20  # ±20% flexibility for drivers

# ========================= MODELS =========================

class LocationPoint(BaseModel):
    lat: float
    lng: float
    address: Optional[str] = None

class RideRequestCreate(BaseModel):
    pickup: LocationPoint
    drop: LocationPoint
    vehicle_type: str = "auto"

class FareProposalCreate(BaseModel):
    ride_id: str
    fare_amount: float

class CounterOfferCreate(BaseModel):
    proposal_id: str
    counter_amount: float

class ChatMessageCreate(BaseModel):
    ride_id: str
    receiver_id: str
    message: str
    original_language: str = "en"
    target_language: str = "en"

class DriverLocationUpdate(BaseModel):
    lat: float
    lng: float

class BookRideRequest(BaseModel):
    proposal_id: str

class OTPVerify(BaseModel):
    ride_id: str
    otp: str

class EmergencyContactCreate(BaseModel):
    name: str
    phone: str
    relationship: str

class SOSAlertCreate(BaseModel):
    ride_id: Optional[str] = None
    location_lat: float
    location_lng: float
    message: Optional[str] = None

class ScheduledRideCreate(BaseModel):
    pickup: LocationPoint
    drop: LocationPoint
    vehicle_type: str = "auto"
    scheduled_time: str
    notes: Optional[str] = None

class RideRatingCreate(BaseModel):
    ride_id: str
    rating: int
    feedback: Optional[str] = None

class PaymentOrderCreate(BaseModel):
    ride_id: str
    amount: float

class PaymentVerify(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    ride_id: str

# ========================= ROUTING & FARE CALCULATION =========================

async def get_route_from_osrm(pickup: LocationPoint, drop: LocationPoint) -> dict:
    """Get actual driving route from OSRM (Open Source Routing Machine)"""
    try:
        # OSRM expects coordinates as lng,lat
        url = f"https://router.project-osrm.org/route/v1/driving/{pickup.lng},{pickup.lat};{drop.lng},{drop.lat}"
        params = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "false"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            data = response.json()
            
            if data.get("code") == "Ok" and data.get("routes"):
                route = data["routes"][0]
                distance_meters = route["distance"]
                duration_seconds = route["duration"]
                geometry = route["geometry"]  # GeoJSON LineString
                
                return {
                    "distance_km": round(distance_meters / 1000, 2),
                    "duration_minutes": round(duration_seconds / 60, 1),
                    "route_geometry": geometry,
                    "success": True
                }
    except Exception as e:
        logger.error(f"OSRM routing error: {e}")
    
    # Fallback to straight-line distance if OSRM fails
    from math import radians, sin, cos, sqrt, atan2
    R = 6371  # Earth's radius in km
    lat1, lon1 = radians(pickup.lat), radians(pickup.lng)
    lat2, lon2 = radians(drop.lat), radians(drop.lng)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    distance = R * c
    
    # Multiply by 1.3 to approximate road distance
    road_distance = distance * 1.3
    
    return {
        "distance_km": round(road_distance, 2),
        "duration_minutes": round(road_distance * 2.5, 1),  # Assume ~24 km/h avg speed
        "route_geometry": None,
        "success": False
    }

def calculate_fare(distance_km: float) -> dict:
    """Calculate fare based on actual road distance"""
    base_fare = BASE_FARE + (PER_KM_RATE * distance_km)
    base_fare = round(base_fare, 2)
    min_fare = round(base_fare * (1 - FARE_FLEXIBILITY), 2)
    max_fare = round(base_fare * (1 + FARE_FLEXIBILITY), 2)
    
    return {
        "base_fare": base_fare,
        "min_fare": min_fare,
        "max_fare": max_fare
    }

def generate_otp() -> str:
    return ''.join(random.choices(string.digits, k=4))

# ========================= AUTH HELPERS =========================

async def get_current_user(request: Request) -> dict:
    session_token = request.cookies.get("session_token")
    if not session_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            session_token = auth_header.split(" ")[1]
    
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    session = await db.user_sessions.find_one({"session_token": session_token}, {"_id": 0})
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    expires_at = session.get("expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")
    
    user = await db.users.find_one({"user_id": session["user_id"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    return user

# ========================= AUTH ROUTES =========================

@api_router.get("/auth/session")
async def exchange_session(request: Request, response: Response):
    session_id = request.headers.get("X-Session-ID")
    if not session_id:
        raise HTTPException(status_code=400, detail="Session ID required")
    
    async with httpx.AsyncClient() as client_http:
        resp = await client_http.get(
            "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
            headers={"X-Session-ID": session_id}
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid session ID")
        data = resp.json()
    
    existing_user = await db.users.find_one({"email": data["email"]}, {"_id": 0})
    if existing_user:
        user_id = existing_user["user_id"]
        await db.users.update_one(
            {"user_id": user_id},
            {"$set": {"name": data["name"], "picture": data.get("picture")}}
        )
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        new_user = {
            "user_id": user_id,
            "name": data["name"],
            "email": data["email"],
            "picture": data.get("picture"),
            "user_type": "passenger",
            "preferred_language": "en",
            "phone": None,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.users.insert_one(new_user)
    
    session_token = data.get("session_token", f"sess_{uuid.uuid4().hex}")
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    
    await db.user_sessions.delete_many({"user_id": user_id})
    await db.user_sessions.insert_one({
        "user_id": user_id,
        "session_token": session_token,
        "expires_at": expires_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    
    response.set_cookie(
        key="session_token", value=session_token, httponly=True,
        secure=True, samesite="none", path="/", max_age=7*24*60*60
    )
    
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    return user

@api_router.get("/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    return user

@api_router.post("/auth/logout")
async def logout(request: Request, response: Response):
    session_token = request.cookies.get("session_token")
    if session_token:
        await db.user_sessions.delete_many({"session_token": session_token})
    response.delete_cookie("session_token", path="/")
    return {"message": "Logged out"}

@api_router.put("/auth/profile")
async def update_profile(
    phone: Optional[str] = None,
    user_type: Optional[str] = None,
    preferred_language: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    update_data = {}
    if phone:
        update_data["phone"] = phone
    if user_type in ["passenger", "driver"]:
        update_data["user_type"] = user_type
    if preferred_language:
        update_data["preferred_language"] = preferred_language
    
    if update_data:
        await db.users.update_one({"user_id": user["user_id"]}, {"$set": update_data})
    
    updated_user = await db.users.find_one({"user_id": user["user_id"]}, {"_id": 0})
    return updated_user

# ========================= RIDE ROUTES =========================

@api_router.post("/rides/request")
async def create_ride_request(data: RideRequestCreate, user: dict = Depends(get_current_user)):
    """Create a new ride request with actual route calculation"""
    ride_id = f"ride_{uuid.uuid4().hex[:12]}"
    
    # Get actual route from OSRM
    route_info = await get_route_from_osrm(data.pickup, data.drop)
    distance_km = route_info["distance_km"]
    
    # Calculate fare based on actual road distance
    fare_info = calculate_fare(distance_km)
    
    ride = {
        "ride_id": ride_id,
        "passenger_id": user["user_id"],
        "passenger_name": user["name"],
        "passenger_phone": user.get("phone"),
        "pickup": data.pickup.model_dump(),
        "drop": data.drop.model_dump(),
        "vehicle_type": data.vehicle_type,
        "status": "pending",
        "distance_km": distance_km,
        "duration_minutes": route_info["duration_minutes"],
        "route_geometry": route_info["route_geometry"],
        "base_fare": fare_info["base_fare"],
        "min_fare": fare_info["min_fare"],
        "max_fare": fare_info["max_fare"],
        "otp": None,
        "booked_proposal_id": None,
        "payment_status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.rides.insert_one(ride)
    ride.pop("_id", None)
    
    # Hide fare flexibility from passenger
    response = {k: v for k, v in ride.items() if k not in ["min_fare", "max_fare"]}
    return response

@api_router.get("/rides/active")
async def get_active_rides(user: dict = Depends(get_current_user)):
    if user["user_type"] == "passenger":
        rides = await db.rides.find(
            {"passenger_id": user["user_id"], "status": {"$nin": ["completed", "cancelled"]}},
            {"_id": 0, "min_fare": 0, "max_fare": 0}
        ).to_list(100)
    else:
        rides = await db.rides.find(
            {"driver_id": user["user_id"], "status": {"$in": ["booked", "in_progress"]}},
            {"_id": 0}
        ).to_list(100)
    return rides

@api_router.get("/rides/{ride_id}")
async def get_ride(ride_id: str, user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"ride_id": ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    # Hide fare flexibility from passengers
    if user["user_type"] == "passenger" or ride.get("passenger_id") == user["user_id"]:
        ride.pop("min_fare", None)
        ride.pop("max_fare", None)
    
    return ride

@api_router.get("/rides/{ride_id}/route")
async def get_ride_route(ride_id: str, user: dict = Depends(get_current_user)):
    """Get the route geometry for a ride"""
    ride = await db.rides.find_one({"ride_id": ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    return {
        "route_geometry": ride.get("route_geometry"),
        "distance_km": ride.get("distance_km"),
        "duration_minutes": ride.get("duration_minutes"),
        "pickup": ride.get("pickup"),
        "drop": ride.get("drop")
    }

@api_router.get("/rides/{ride_id}/proposals")
async def get_ride_proposals(ride_id: str, user: dict = Depends(get_current_user)):
    proposals = await db.fare_proposals.find(
        {"ride_id": ride_id}, {"_id": 0}
    ).sort("created_at", -1).to_list(50)
    return proposals

@api_router.post("/rides/{ride_id}/cancel")
async def cancel_ride(ride_id: str, user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"ride_id": ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride["passenger_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    await db.rides.update_one({"ride_id": ride_id}, {"$set": {"status": "cancelled"}})
    return {"message": "Ride cancelled"}

@api_router.get("/rides/history/all")
async def get_ride_history(user: dict = Depends(get_current_user)):
    if user["user_type"] == "passenger":
        rides = await db.rides.find(
            {"passenger_id": user["user_id"]}, {"_id": 0, "min_fare": 0, "max_fare": 0}
        ).sort("created_at", -1).to_list(100)
    else:
        rides = await db.rides.find(
            {"driver_id": user["user_id"]}, {"_id": 0}
        ).sort("created_at", -1).to_list(100)
    return rides

@api_router.post("/rides/book")
async def book_ride(data: BookRideRequest, user: dict = Depends(get_current_user)):
    proposal = await db.fare_proposals.find_one({"proposal_id": data.proposal_id}, {"_id": 0})
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    
    ride = await db.rides.find_one({"ride_id": proposal["ride_id"]}, {"_id": 0})
    if ride["passenger_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    otp = generate_otp()
    
    await db.rides.update_one(
        {"ride_id": proposal["ride_id"]},
        {"$set": {
            "status": "booked",
            "booked_proposal_id": data.proposal_id,
            "driver_id": proposal["driver_id"],
            "driver_name": proposal["driver_name"],
            "driver_phone": proposal.get("driver_phone"),
            "final_fare": proposal["fare_amount"],
            "otp": otp,
            "booked_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    await db.fare_proposals.update_one({"proposal_id": data.proposal_id}, {"$set": {"status": "accepted"}})
    await db.fare_proposals.update_many(
        {"ride_id": proposal["ride_id"], "proposal_id": {"$ne": data.proposal_id}},
        {"$set": {"status": "rejected"}}
    )
    
    updated_ride = await db.rides.find_one({"ride_id": proposal["ride_id"]}, {"_id": 0, "min_fare": 0, "max_fare": 0})
    return updated_ride

@api_router.post("/rides/verify-otp")
async def verify_otp(data: OTPVerify, user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"ride_id": data.ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.get("driver_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    if ride.get("otp") != data.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    
    await db.rides.update_one(
        {"ride_id": data.ride_id},
        {"$set": {"status": "in_progress", "started_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"message": "OTP verified, ride started"}

@api_router.post("/rides/{ride_id}/complete")
async def complete_ride(ride_id: str, user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"ride_id": ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.get("driver_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    await db.rides.update_one(
        {"ride_id": ride_id},
        {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc).isoformat()}}
    )
    await db.driver_profiles.update_one({"user_id": user["user_id"]}, {"$inc": {"total_rides": 1}})
    return {"message": "Ride completed"}

# ========================= DRIVER ROUTES =========================

@api_router.get("/driver/nearby-rides")
async def get_nearby_rides(lat: float, lng: float, user: dict = Depends(get_current_user)):
    if user["user_type"] != "driver":
        raise HTTPException(status_code=403, detail="Only drivers can view nearby rides")
    
    rides = await db.rides.find({"status": "pending"}, {"_id": 0}).to_list(100)
    
    from math import radians, sin, cos, sqrt, atan2
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        return R * 2 * atan2(sqrt(a), sqrt(1-a))
    
    nearby_rides = []
    for ride in rides:
        pickup = ride["pickup"]
        distance = haversine(lat, lng, pickup["lat"], pickup["lng"])
        if distance <= 20:
            ride["distance_from_driver"] = round(distance, 2)
            nearby_rides.append(ride)
    
    return sorted(nearby_rides, key=lambda x: x["distance_from_driver"])

@api_router.post("/driver/propose-fare")
async def propose_fare(data: FareProposalCreate, user: dict = Depends(get_current_user)):
    if user["user_type"] != "driver":
        raise HTTPException(status_code=403, detail="Only drivers can propose fares")
    
    ride = await db.rides.find_one({"ride_id": data.ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    max_fare = ride.get("max_fare")
    if max_fare and data.fare_amount > max_fare:
        raise HTTPException(status_code=400, detail=f"Fare cannot exceed ₹{max_fare}")
    
    existing = await db.fare_proposals.find_one({
        "ride_id": data.ride_id, "driver_id": user["user_id"]
    })
    
    if existing:
        await db.fare_proposals.update_one(
            {"proposal_id": existing["proposal_id"]},
            {"$set": {"fare_amount": data.fare_amount, "status": "pending"}}
        )
        updated = await db.fare_proposals.find_one({"proposal_id": existing["proposal_id"]}, {"_id": 0})
        return updated
    
    proposal_id = f"prop_{uuid.uuid4().hex[:12]}"
    driver_info = await db.driver_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    
    proposal = {
        "proposal_id": proposal_id,
        "ride_id": data.ride_id,
        "driver_id": user["user_id"],
        "driver_name": user["name"],
        "driver_phone": user.get("phone"),
        "driver_rating": driver_info.get("rating", 4.5) if driver_info else 4.5,
        "fare_amount": data.fare_amount,
        "status": "pending",
        "vehicle_type": driver_info.get("vehicle_type", "auto") if driver_info else "auto",
        "vehicle_number": driver_info.get("vehicle_number") if driver_info else None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.fare_proposals.insert_one(proposal)
    await db.rides.update_one({"ride_id": data.ride_id}, {"$set": {"status": "proposals"}})
    
    proposal.pop("_id", None)
    return proposal

@api_router.post("/driver/update-location")
async def update_driver_location(data: DriverLocationUpdate, user: dict = Depends(get_current_user)):
    if user["user_type"] != "driver":
        raise HTTPException(status_code=403, detail="Only drivers can update location")
    
    await db.driver_locations.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "user_id": user["user_id"],
            "lat": data.lat, "lng": data.lng,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }},
        upsert=True
    )
    return {"message": "Location updated"}

@api_router.put("/driver/profile")
async def update_driver_profile(vehicle_type: str, vehicle_number: str, user: dict = Depends(get_current_user)):
    await db.driver_profiles.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "user_id": user["user_id"],
            "vehicle_type": vehicle_type,
            "vehicle_number": vehicle_number,
            "rating": 4.5,
            "total_rides": 0,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }},
        upsert=True
    )
    return {"message": "Profile updated"}

@api_router.get("/driver/profile")
async def get_driver_profile(user: dict = Depends(get_current_user)):
    profile = await db.driver_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    return profile or {}

@api_router.get("/rides/{ride_id}/driver-location")
async def get_driver_location_for_ride(ride_id: str, user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"ride_id": ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    driver_id = ride.get("driver_id")
    if not driver_id:
        raise HTTPException(status_code=404, detail="No driver assigned")
    
    location = await db.driver_locations.find_one({"user_id": driver_id}, {"_id": 0})
    if not location:
        raise HTTPException(status_code=404, detail="Driver location not available")
    
    return location

# ========================= NEGOTIATION ROUTES =========================

@api_router.post("/negotiate/counter-offer")
async def create_counter_offer(data: CounterOfferCreate, user: dict = Depends(get_current_user)):
    proposal = await db.fare_proposals.find_one({"proposal_id": data.proposal_id}, {"_id": 0})
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    
    ride = await db.rides.find_one({"ride_id": proposal["ride_id"]}, {"_id": 0})
    if ride["passenger_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    await db.fare_proposals.update_one(
        {"proposal_id": data.proposal_id},
        {"$set": {
            "counter_amount": data.counter_amount,
            "status": "countered",
            "countered_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    await db.rides.update_one({"ride_id": proposal["ride_id"]}, {"$set": {"status": "negotiating"}})
    
    updated = await db.fare_proposals.find_one({"proposal_id": data.proposal_id}, {"_id": 0})
    return updated

# ========================= CHAT ROUTES =========================

@api_router.post("/chat/send")
async def send_message(data: ChatMessageCreate, user: dict = Depends(get_current_user)):
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    
    translated_message = None
    if data.original_language != data.target_language and EMERGENT_LLM_KEY:
        try:
            from emergentintegrations.llm.chat import LlmChat, UserMessage
            chat = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=f"translate_{message_id}",
                system_message="You are a translator. Translate accurately. Only return the translation."
            ).with_model("openai", "gpt-5.2")
            
            lang_map = {"en": "English", "hi": "Hindi", "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "bn": "Bengali"}
            user_msg = UserMessage(text=f"Translate from {lang_map.get(data.original_language, 'English')} to {lang_map.get(data.target_language, 'English')}: {data.message}")
            translated_message = await chat.send_message(user_msg)
        except Exception as e:
            logger.error(f"Translation error: {e}")
            translated_message = data.message
    
    msg = {
        "message_id": message_id,
        "ride_id": data.ride_id,
        "sender_id": user["user_id"],
        "sender_name": user["name"],
        "receiver_id": data.receiver_id,
        "message": data.message,
        "translated_message": translated_message,
        "original_language": data.original_language,
        "target_language": data.target_language,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.chat_messages.insert_one(msg)
    msg.pop("_id", None)
    return msg

@api_router.get("/chat/{ride_id}")
async def get_chat_messages(ride_id: str, user: dict = Depends(get_current_user)):
    messages = await db.chat_messages.find({"ride_id": ride_id}, {"_id": 0}).sort("created_at", 1).to_list(500)
    return messages

# ========================= EMERGENCY ROUTES =========================

@api_router.post("/emergency/contacts")
async def add_emergency_contact(data: EmergencyContactCreate, user: dict = Depends(get_current_user)):
    contact_id = f"contact_{uuid.uuid4().hex[:12]}"
    contact = {
        "contact_id": contact_id,
        "user_id": user["user_id"],
        "name": data.name,
        "phone": data.phone,
        "relationship": data.relationship,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.emergency_contacts.insert_one(contact)
    contact.pop("_id", None)
    return contact

@api_router.get("/emergency/contacts")
async def get_emergency_contacts(user: dict = Depends(get_current_user)):
    contacts = await db.emergency_contacts.find({"user_id": user["user_id"]}, {"_id": 0}).to_list(10)
    return contacts

@api_router.delete("/emergency/contacts/{contact_id}")
async def delete_emergency_contact(contact_id: str, user: dict = Depends(get_current_user)):
    result = await db.emergency_contacts.delete_one({"contact_id": contact_id, "user_id": user["user_id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"message": "Contact deleted"}

@api_router.post("/emergency/sos")
async def trigger_sos(data: SOSAlertCreate, user: dict = Depends(get_current_user)):
    alert_id = f"sos_{uuid.uuid4().hex[:12]}"
    contacts = await db.emergency_contacts.find({"user_id": user["user_id"]}, {"_id": 0}).to_list(10)
    
    alert = {
        "alert_id": alert_id,
        "user_id": user["user_id"],
        "user_name": user["name"],
        "location": {"lat": data.location_lat, "lng": data.location_lng},
        "message": data.message,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.sos_alerts.insert_one(alert)
    
    maps_link = f"https://www.google.com/maps?q={data.location_lat},{data.location_lng}"
    
    return {
        "alert_id": alert_id,
        "status": "alert_created",
        "location_link": maps_link,
        "emergency_numbers": {"police": "100", "ambulance": "102", "women_helpline": "1091", "emergency": "112"},
        "emergency_contacts": contacts
    }

# ========================= SCHEDULED RIDES =========================

@api_router.post("/rides/schedule")
async def create_scheduled_ride(data: ScheduledRideCreate, user: dict = Depends(get_current_user)):
    scheduled_id = f"sched_{uuid.uuid4().hex[:12]}"
    
    try:
        scheduled_dt = datetime.fromisoformat(data.scheduled_time.replace('Z', '+00:00'))
    except:
        raise HTTPException(status_code=400, detail="Invalid datetime format")
    
    if scheduled_dt < datetime.now(timezone.utc) + timedelta(minutes=30):
        raise HTTPException(status_code=400, detail="Must be at least 30 min in future")
    
    # Get route for scheduled ride
    route_info = await get_route_from_osrm(data.pickup, data.drop)
    
    scheduled_ride = {
        "scheduled_id": scheduled_id,
        "passenger_id": user["user_id"],
        "passenger_name": user["name"],
        "pickup": data.pickup.model_dump(),
        "drop": data.drop.model_dump(),
        "vehicle_type": data.vehicle_type,
        "scheduled_time": scheduled_dt.isoformat(),
        "notes": data.notes,
        "status": "scheduled",
        "distance_km": route_info["distance_km"],
        "duration_minutes": route_info["duration_minutes"],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.scheduled_rides.insert_one(scheduled_ride)
    scheduled_ride.pop("_id", None)
    return scheduled_ride

@api_router.get("/rides/scheduled")
async def get_scheduled_rides(user: dict = Depends(get_current_user)):
    scheduled = await db.scheduled_rides.find(
        {"passenger_id": user["user_id"], "status": {"$in": ["scheduled", "active"]}},
        {"_id": 0}
    ).sort("scheduled_time", 1).to_list(50)
    return scheduled

@api_router.post("/rides/scheduled/{scheduled_id}/cancel")
async def cancel_scheduled_ride(scheduled_id: str, user: dict = Depends(get_current_user)):
    scheduled = await db.scheduled_rides.find_one({"scheduled_id": scheduled_id}, {"_id": 0})
    if not scheduled or scheduled["passenger_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Not found")
    
    await db.scheduled_rides.update_one(
        {"scheduled_id": scheduled_id},
        {"$set": {"status": "cancelled"}}
    )
    return {"message": "Cancelled"}

@api_router.post("/rides/scheduled/{scheduled_id}/activate")
async def activate_scheduled_ride(scheduled_id: str, user: dict = Depends(get_current_user)):
    scheduled = await db.scheduled_rides.find_one({"scheduled_id": scheduled_id}, {"_id": 0})
    if not scheduled or scheduled["passenger_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Not found")
    
    ride_id = f"ride_{uuid.uuid4().hex[:12]}"
    
    # Get fresh route
    pickup = LocationPoint(**scheduled["pickup"])
    drop = LocationPoint(**scheduled["drop"])
    route_info = await get_route_from_osrm(pickup, drop)
    fare_info = calculate_fare(route_info["distance_km"])
    
    ride = {
        "ride_id": ride_id,
        "passenger_id": user["user_id"],
        "passenger_name": user["name"],
        "pickup": scheduled["pickup"],
        "drop": scheduled["drop"],
        "vehicle_type": scheduled["vehicle_type"],
        "status": "pending",
        "distance_km": route_info["distance_km"],
        "duration_minutes": route_info["duration_minutes"],
        "route_geometry": route_info["route_geometry"],
        "base_fare": fare_info["base_fare"],
        "min_fare": fare_info["min_fare"],
        "max_fare": fare_info["max_fare"],
        "otp": None,
        "payment_status": "pending",
        "scheduled_id": scheduled_id,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.rides.insert_one(ride)
    await db.scheduled_rides.update_one({"scheduled_id": scheduled_id}, {"$set": {"status": "active", "ride_id": ride_id}})
    
    ride.pop("_id", None)
    ride.pop("min_fare", None)
    ride.pop("max_fare", None)
    return ride

# ========================= RATING ROUTES =========================

@api_router.post("/rides/{ride_id}/rate")
async def rate_ride(ride_id: str, data: RideRatingCreate, user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"ride_id": ride_id}, {"_id": 0})
    if not ride or ride["status"] != "completed":
        raise HTTPException(status_code=400, detail="Can only rate completed rides")
    
    if data.rating < 1 or data.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be 1-5")
    
    rating_id = f"rating_{uuid.uuid4().hex[:12]}"
    
    if user["user_id"] == ride["passenger_id"]:
        rated_user_id = ride.get("driver_id")
        rating_type = "driver_rating"
    else:
        rated_user_id = ride["passenger_id"]
        rating_type = "passenger_rating"
    
    rating_doc = {
        "rating_id": rating_id,
        "ride_id": ride_id,
        "rater_id": user["user_id"],
        "rated_user_id": rated_user_id,
        "rating": data.rating,
        "feedback": data.feedback,
        "rating_type": rating_type,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.ratings.insert_one(rating_doc)
    await db.rides.update_one({"ride_id": ride_id}, {"$set": {rating_type: data.rating}})
    
    if rating_type == "driver_rating" and rated_user_id:
        all_ratings = await db.ratings.find(
            {"rated_user_id": rated_user_id, "rating_type": "driver_rating"}, {"_id": 0}
        ).to_list(1000)
        if all_ratings:
            avg_rating = sum(r["rating"] for r in all_ratings) / len(all_ratings)
            await db.driver_profiles.update_one(
                {"user_id": rated_user_id},
                {"$set": {"rating": round(avg_rating, 2)}}
            )
    
    rating_doc.pop("_id", None)
    return rating_doc

# ========================= MISC =========================

@api_router.get("/")
async def root():
    return {"message": "NammaYatra API", "version": "1.0.0"}

@api_router.get("/health")
async def health():
    return {"status": "healthy"}

# Include the router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
