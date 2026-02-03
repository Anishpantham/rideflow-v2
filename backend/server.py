<<<<<<< HEAD
from fastapi import FastAPI, APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
=======
from fastapi import FastAPI, APIRouter
>>>>>>> 9f0514f (auto-commit for 0ea875e5-1178-499f-9dca-8337937a1861)
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
<<<<<<< HEAD
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional, Literal
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
import random
import string
=======
from pydantic import BaseModel, Field, ConfigDict
from typing import List
import uuid
from datetime import datetime, timezone

>>>>>>> 9f0514f (auto-commit for 0ea875e5-1178-499f-9dca-8337937a1861)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

<<<<<<< HEAD
# JWT configuration
SECRET_KEY = os.environ.get('JWT_SECRET', 'your-secret-key-change-in-production')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# Create the main app
app = FastAPI()
api_router = APIRouter(prefix="/api")
security = HTTPBearer()

# Models
class UserRole(str):
    DRIVER = "driver"
    PASSENGER = "passenger"

class LocationModel(BaseModel):
    lat: float
    lng: float
    address: Optional[str] = None

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str
    phone: str
    role: Literal["driver", "passenger"]
    vehicle_info: Optional[dict] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class RideRequest(BaseModel):
    pickup: LocationModel
    destination: LocationModel
    passenger_id: str

class FareProposal(BaseModel):
    ride_id: str
    proposed_fare: float
    driver_id: str

class FareResponse(BaseModel):
    ride_id: str
    action: Literal["confirm", "negotiate", "cancel"]
    counter_offer: Optional[float] = None

class ChatMessage(BaseModel):
    ride_id: str
    sender_id: str
    message: str
    message_type: Literal["text", "fare_proposal", "fare_confirmed"] = "text"
    fare_amount: Optional[float] = None

class LocationUpdate(BaseModel):
    user_id: str
    location: LocationModel

class OTPVerification(BaseModel):
    ride_id: str
    otp: str

# Helper functions
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def generate_otp() -> str:
    return ''.join(random.choices(string.digits, k=6))

# Routes
@api_router.get("/")
async def root():
    return {"message": "RideFlow API"}

@api_router.post("/auth/register")
async def register(request: RegisterRequest):
    # Check if user exists
    existing_user = await db.users.find_one({"email": request.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = str(uuid.uuid4())
    user_doc = {
        "id": user_id,
        "email": request.email,
        "password": hash_password(request.password),
        "name": request.name,
        "phone": request.phone,
        "role": request.role,
        "vehicle_info": request.vehicle_info if request.role == "driver" else None,
        "current_location": None,
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    print(user_doc)
    
    await db.users.insert_one(user_doc)
    
    token = create_access_token({"sub": user_id, "email": request.email, "role": request.role})
    
    return {
        "token": token,
        "user": {
            "id": user_id,
            "email": request.email,
            "name": request.name,
            "phone": request.phone,
            "role": request.role,
        }
    }

@api_router.post("/auth/login")
async def login(request: LoginRequest):
    user = await db.users.find_one({"email": request.email})
    if not user or not verify_password(request.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    token = create_access_token({"sub": user["id"], "email": user["email"], "role": user["role"]})
    
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "phone": user["phone"],
            "role": user["role"],
            "vehicle_info": user.get("vehicle_info"),
        }
    }

@api_router.get("/auth/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "name": current_user["name"],
        "phone": current_user["phone"],
        "role": current_user["role"],
        "vehicle_info": current_user.get("vehicle_info"),
        "current_location": current_user.get("current_location"),
    }

@api_router.post("/rides/request")
async def request_ride(request: RideRequest, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "passenger":
        raise HTTPException(status_code=403, detail="Only passengers can request rides")
    
    ride_id = str(uuid.uuid4())
    ride_doc = {
        "id": ride_id,
        "passenger_id": current_user["id"],
        "passenger_name": current_user["name"],
        "passenger_phone": current_user["phone"],
        "pickup": request.pickup.model_dump(),
        "destination": request.destination.model_dump(),
        "status": "pending",
        "driver_id": None,
        "proposed_fare": None,
        "agreed_fare": None,
        "otp": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
    }
    
    await db.rides.insert_one(ride_doc)
    
    return {"ride_id": ride_id, "message": "Ride requested successfully", "status": "pending"}

@api_router.get("/rides/available")
async def get_available_rides(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "driver":
        raise HTTPException(status_code=403, detail="Only drivers can view available rides")
    
    rides = await db.rides.find(
        {"status": "pending"},
        {"_id": 0}
    ).to_list(100)
    
    return {"rides": rides}

@api_router.get("/rides/my-rides")
async def get_my_rides(current_user: dict = Depends(get_current_user)):
    if current_user["role"] == "driver":
        query = {"driver_id": current_user["id"]}
    else:
        query = {"passenger_id": current_user["id"]}
    
    rides = await db.rides.find(query, {"_id": 0}).sort("created_at", -1).to_list(50)
    
    return {"rides": rides}

@api_router.get("/rides/{ride_id}")
async def get_ride(ride_id: str, current_user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"id": ride_id}, {"_id": 0})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    # Check if user is part of this ride
    if ride.get("passenger_id") != current_user["id"] and ride.get("driver_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return ride

@api_router.post("/rides/propose-fare")
async def propose_fare(proposal: FareProposal, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "driver":
        raise HTTPException(status_code=403, detail="Only drivers can propose fares")
    
    ride = await db.rides.find_one({"id": proposal.ride_id})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    if ride["status"] != "pending":
        raise HTTPException(status_code=400, detail="Ride is not available for fare proposal")
    
    # Update ride with fare proposal and driver
    await db.rides.update_one(
        {"id": proposal.ride_id},
        {
            "$set": {
                "driver_id": current_user["id"],
                "driver_name": current_user["name"],
                "driver_phone": current_user["phone"],
                "driver_vehicle": current_user.get("vehicle_info"),
                "proposed_fare": proposal.proposed_fare,
                "status": "fare_proposed",
            }
        }
    )
    
    # Create chat message for fare proposal
    message_doc = {
        "id": str(uuid.uuid4()),
        "ride_id": proposal.ride_id,
        "sender_id": current_user["id"],
        "sender_name": current_user["name"],
        "sender_role": current_user["role"],
        "message": f"Proposed fare: ${proposal.proposed_fare:.2f}",
        "message_type": "fare_proposal",
        "fare_amount": proposal.proposed_fare,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await db.messages.insert_one(message_doc)
    
    return {"message": "Fare proposed successfully", "proposed_fare": proposal.proposed_fare}

@api_router.post("/rides/respond-fare")
async def respond_to_fare(response: FareResponse, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "passenger":
        raise HTTPException(status_code=403, detail="Only passengers can respond to fare")
    
    ride = await db.rides.find_one({"id": response.ride_id})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    if ride["passenger_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if response.action == "confirm":
        # Generate OTP and start ride
        otp = generate_otp()
        await db.rides.update_one(
            {"id": response.ride_id},
            {
                "$set": {
                    "status": "in_progress",
                    "agreed_fare": ride["proposed_fare"],
                    "otp": otp,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        )
        
        # Create chat message
        message_doc = {
            "id": str(uuid.uuid4()),
            "ride_id": response.ride_id,
            "sender_id": current_user["id"],
            "sender_name": current_user["name"],
            "sender_role": current_user["role"],
            "message": f"Fare confirmed at ${ride['proposed_fare']:.2f}",
            "message_type": "fare_confirmed",
            "fare_amount": ride["proposed_fare"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await db.messages.insert_one(message_doc)
        
        return {"message": "Ride confirmed and started", "otp": otp, "status": "in_progress"}
    
    elif response.action == "negotiate":
        # Update status to negotiating
        await db.rides.update_one(
            {"id": response.ride_id},
            {"$set": {"status": "negotiating"}}
        )
        
        if response.counter_offer:
            # Create chat message with counter offer
            message_doc = {
                "id": str(uuid.uuid4()),
                "ride_id": response.ride_id,
                "sender_id": current_user["id"],
                "sender_name": current_user["name"],
                "sender_role": current_user["role"],
                "message": f"Counter offer: ${response.counter_offer:.2f}",
                "message_type": "fare_proposal",
                "fare_amount": response.counter_offer,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await db.messages.insert_one(message_doc)
        
        return {"message": "Negotiation started", "status": "negotiating"}
    
    else:  # cancel
        await db.rides.update_one(
            {"id": response.ride_id},
            {"$set": {"status": "cancelled", "driver_id": None, "proposed_fare": None}}
        )
        
        return {"message": "Ride cancelled", "status": "cancelled"}

@api_router.post("/rides/update-fare")
async def update_fare(proposal: FareProposal, current_user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"id": proposal.ride_id})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    # Update proposed fare
    await db.rides.update_one(
        {"id": proposal.ride_id},
        {"$set": {"proposed_fare": proposal.proposed_fare, "status": "fare_proposed"}}
    )
    
    # Create chat message
    message_doc = {
        "id": str(uuid.uuid4()),
        "ride_id": proposal.ride_id,
        "sender_id": current_user["id"],
        "sender_name": current_user["name"],
        "sender_role": current_user["role"],
        "message": f"New fare offer: ${proposal.proposed_fare:.2f}",
        "message_type": "fare_proposal",
        "fare_amount": proposal.proposed_fare,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await db.messages.insert_one(message_doc)
    
    return {"message": "Fare updated", "proposed_fare": proposal.proposed_fare}

@api_router.post("/chat/send")
async def send_message(message: ChatMessage, current_user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"id": message.ride_id})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    message_doc = {
        "id": str(uuid.uuid4()),
        "ride_id": message.ride_id,
        "sender_id": current_user["id"],
        "sender_name": current_user["name"],
        "sender_role": current_user["role"],
        "message": message.message,
        "message_type": message.message_type,
        "fare_amount": message.fare_amount,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    await db.messages.insert_one(message_doc)
    
    return {"message_id": message_doc["id"], "timestamp": message_doc["timestamp"]}

@api_router.get("/chat/{ride_id}")
async def get_messages(ride_id: str, current_user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"id": ride_id})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    messages = await db.messages.find(
        {"ride_id": ride_id},
        {"_id": 0}
    ).sort("timestamp", 1).to_list(500)
    
    return {"messages": messages}

@api_router.post("/location/update")
async def update_location(update: LocationUpdate, current_user: dict = Depends(get_current_user)):
    await db.users.update_one(
        {"id": current_user["id"]},
        {
            "$set": {
                "current_location": update.location.model_dump(),
                "location_updated_at": datetime.now(timezone.utc).isoformat(),
            }
        }
    )
    
    return {"message": "Location updated"}

@api_router.get("/location/{user_id}")
async def get_location(user_id: str, current_user: dict = Depends(get_current_user)):
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {
        "user_id": user_id,
        "current_location": user.get("current_location"),
        "location_updated_at": user.get("location_updated_at"),
    }

@api_router.post("/rides/verify-otp")
async def verify_otp(verification: OTPVerification, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "driver":
        raise HTTPException(status_code=403, detail="Only drivers can verify OTP")
    
    ride = await db.rides.find_one({"id": verification.ride_id})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    if ride["driver_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if ride["otp"] != verification.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    
    # Complete the ride
    await db.rides.update_one(
        {"id": verification.ride_id},
        {
            "$set": {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        }
    )
    
    return {"message": "Ride completed successfully", "status": "completed"}

@api_router.post("/payment/process")
async def process_payment(ride_id: str, current_user: dict = Depends(get_current_user)):
    ride = await db.rides.find_one({"id": ride_id})
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    
    if ride["status"] != "completed":
        raise HTTPException(status_code=400, detail="Ride must be completed before payment")
    
    # Mock payment processing
    payment_id = str(uuid.uuid4())
    payment_doc = {
        "id": payment_id,
        "ride_id": ride_id,
        "amount": ride["agreed_fare"],
        "passenger_id": ride["passenger_id"],
        "driver_id": ride["driver_id"],
        "status": "completed",
        "payment_method": "mock",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    await db.payments.insert_one(payment_doc)
    await db.rides.update_one(
        {"id": ride_id},
        {"$set": {"payment_status": "paid", "payment_id": payment_id}}
    )
    
    return {"payment_id": payment_id, "message": "Payment processed successfully", "amount": ride["agreed_fare"]}

# Include router
=======
# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore MongoDB's _id field
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    
    # Convert to dict and serialize datetime to ISO string for MongoDB
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    _ = await db.status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    # Exclude MongoDB's _id field from the query results
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    
    # Convert ISO string timestamps back to datetime objects
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    
    return status_checks

# Include the router in the main app
>>>>>>> 9f0514f (auto-commit for 0ea875e5-1178-499f-9dca-8337937a1861)
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

<<<<<<< HEAD
=======
# Configure logging
>>>>>>> 9f0514f (auto-commit for 0ea875e5-1178-499f-9dca-8337937a1861)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()