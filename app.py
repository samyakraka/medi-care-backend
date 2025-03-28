from flask import Flask, request, jsonify
from flask_cors import CORS
import os
from groq import Groq
import requests
import base64
import re
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
import random
import logging

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# CORS Configuration - Update with your actual frontend URLs
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "https://your-vercel-app.vercel.app",  # Replace with your production frontend URL
            "http://localhost:3000"               # For local development
        ],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# Initialize Firebase
try:
    cred = credentials.Certificate("medicare11-firebase-adminsdk-fbsvc-8c856c8938.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase initialized successfully")
except Exception as e:
    logger.error(f"Firebase initialization failed: {str(e)}")
    raise e

# API Keys - Load from environment variables
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# System prompt for appointment booking
SYSTEM_PROMPT = """You are an AI medical appointment assistant. Help users:
1. Book appointments by collecting: specialty, doctor, date/time, reason
2. Confirm details before booking
3. Process payments when needed
4. Provide clear, concise responses
5. Always verify information before proceeding

Available commands:
- [BOOK_APPOINTMENT] - When ready to book
- [REQUEST_PAYMENT] - When payment is needed
- [GENERATE_OTP] - Generate a 6-digit OTP for payment verification
- [CONFIRM_DETAILS] - To verify information

Keep responses short and conversational."""

# Helper Functions
def text_to_speech(text: str) -> str:
    """Convert text to speech using ElevenLabs API"""
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        }
        data = {
            "text": text,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.5
            }
        }
        
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()
        return base64.b64encode(response.content).decode('utf-8')
    except Exception as e:
        logger.error(f"ElevenLabs API error: {str(e)}")
        raise Exception(f"Text-to-speech conversion failed: {str(e)}")

def get_specialties() -> list:
    """Get all available specialties from Firebase"""
    try:
        docs = db.collection('doctors').stream()
        specialties = set()
        for doc in docs:
            specialties.add(doc.to_dict().get('specialty', ''))
        return list(specialties)
    except Exception as e:
        logger.error(f"Error getting specialties: {str(e)}")
        return []

def get_doctors_by_specialty(specialty: str) -> list:
    """Get doctors by specialty from Firebase"""
    try:
        docs = db.collection('doctors').where('specialty', '==', specialty).stream()
        return [{'id': doc.id, **doc.to_dict()} for doc in docs]
    except Exception as e:
        logger.error(f"Error getting doctors by specialty: {str(e)}")
        return []

def check_doctor_availability(doctor_id: str, date: str, time: str) -> bool:
    """Check if doctor is available at given date/time"""
    try:
        start_time = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        end_time = start_time + timedelta(minutes=30)
        
        time_slots_ref = db.collection('timeSlots').where('doctorId', '==', doctor_id) \
            .where('date', '==', date) \
            .where('startTime', '==', time) \
            .where('isBooked', '==', False) \
            .limit(1).stream()
        
        return len(list(time_slots_ref)) > 0
    except Exception as e:
        logger.error(f"Error checking doctor availability: {str(e)}")
        return False

def get_patient_by_email(email: str) -> dict:
    """Get patient document by email"""
    try:
        docs = db.collection('patients').where('email', '==', email).limit(1).stream()
        for doc in docs:
            return {'id': doc.id, **doc.to_dict()}
        return None
    except Exception as e:
        logger.error(f"Error getting patient by email: {str(e)}")
        return None

def book_appointment(patient_info: dict, appointment_details: dict) -> dict:
    """Book appointment in Firebase"""
    try:
        if not check_doctor_availability(
            appointment_details['doctorId'],
            appointment_details['date'],
            appointment_details['startTime']
        ):
            return {
                "success": False,
                "error": "Time slot no longer available. Please choose another time."
            }
        
        doctor_ref = db.collection('doctors').document(appointment_details['doctorId'])
        doctor = doctor_ref.get().to_dict()
        
        if not doctor:
            return {
                "success": False,
                "error": "Doctor not found. Please try again."
            }
        
        cost = float(doctor['consultationFees'].get(
            'inPerson' if appointment_details['type'] == 'in-person' else 'telemedicine',
            '100'
        ))
        
        appointment_data = {
            'createdAt': datetime.now().isoformat(),
            'date': appointment_details['date'],
            'doctorId': appointment_details['doctorId'],
            'doctorName': doctor['name'],
            'endTime': appointment_details['endTime'],
            'notes': '',
            'patientEmail': patient_info['email'],
            'patientId': patient_info['id'],
            'patientName': patient_info['name'],
            'reason': appointment_details['reason'],
            'specialty': doctor['specialty'],
            'startTime': appointment_details['startTime'],
            'status': 'pending_payment',
            'type': appointment_details['type'],
            'updatedAt': datetime.now().isoformat(),
            'cost': cost
        }
        
        appointment_ref = db.collection('appointments').add(appointment_data)
        
        # Mark time slot as booked
        time_slots_ref = db.collection('timeSlots').where('doctorId', '==', appointment_details['doctorId']) \
            .where('date', '==', appointment_details['date']) \
            .where('startTime', '==', appointment_details['startTime']) \
            .limit(1).stream()
        
        for slot in time_slots_ref:
            db.collection('timeSlots').document(slot.id).update({'isBooked': True})
        
        # Generate OTP for payment verification
        otp = str(random.randint(100000, 999999))
        
        return {
            "success": True,
            "appointmentId": appointment_ref[1].id,
            "doctorName": doctor['name'],
            "date": appointment_details['date'],
            "time": appointment_details['startTime'],
            "cost": cost,
            "otp": otp,
            "confirmationNumber": f"APT-{datetime.now().strftime('%Y%m%d')}-{appointment_ref[1].id[:6]}"
        }
    
    except Exception as e:
        logger.error(f"Error booking appointment: {str(e)}")
        return {
            "success": False,
            "error": f"Failed to book appointment: {str(e)}"
        }

def process_payment(patient_email: str, amount: float, appointment_id: str, otp: str) -> dict:
    """Process payment in Firebase with OTP verification"""
    try:
        if not otp or len(otp) != 6 or not otp.isdigit():
            return {
                "success": False,
                "error": "Invalid OTP. Please enter a valid 6-digit code."
            }
        
        # Get patient by email
        patient = get_patient_by_email(patient_email)
        if not patient:
            return {
                "success": False,
                "error": "Patient not found. Please login again."
            }
        
        current_balance = float(patient.get('balance', 0))
        
        if current_balance < amount:
            return {
                "success": False,
                "error": "Insufficient funds. Please add money to your wallet."
            }
        
        # Get appointment
        appointment_ref = db.collection('appointments').document(appointment_id)
        appointment = appointment_ref.get().to_dict()
        
        if not appointment:
            return {
                "success": False,
                "error": "Appointment not found. Please try booking again."
            }
        
        # Deduct from balance and update patient
        new_balance = current_balance - amount
        db.collection('patients').document(patient['id']).update({
            'balance': new_balance,
            'updatedAt': datetime.now().isoformat()
        })
        
        # Record transaction
        transaction_data = {
            'amount': amount,
            'balanceAfter': new_balance,
            'description': f"Appointment payment - {appointment_id}",
            'timestamp': datetime.now(),
            'type': 'appointment',
            'userId': patient['id'],
            'status': 'completed',
            'otp': otp,
            'patientEmail': patient_email
        }
        transaction_ref = db.collection('transactions').add(transaction_data)
        
        # Update appointment status
        appointment_ref.update({
            'isPaid': True,
            'paymentDate': datetime.now().isoformat(),
            'status': 'confirmed',
            'transactionId': transaction_ref[1].id
        })
        
        return {
            'success': True,
            'newBalance': new_balance,
            'transactionId': transaction_ref[1].id,
            'message': 'Payment successful! Your appointment is confirmed.'
        }
    
    except Exception as e:
        logger.error(f"Error processing payment: {str(e)}")
        return {
            'success': False,
            'error': f"Payment processing failed: {str(e)}"
        }

def parse_appointment_details(text: str) -> dict:
    """Parse appointment details from text"""
    patterns = {
        "specialty": r"special(?:ty|ies)[:\s]*([^\n]+)",
        "doctorId": r"doctor id[:\s]*([^\n]+)",
        "doctorName": r"doctor[:\s]*([^\n]+)",
        "date": r"date[:\s]*([^\n]+)",
        "startTime": r"time[:\s]*([^\n]+)",
        "reason": r"reason[:\s]*([^\n]+)",
        "type": r"type[:\s]*(in-person|telemedicine)"
    }
    
    details = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            details[key] = match.group(1).strip()
    
    if details.get('startTime'):
        try:
            start_dt = datetime.strptime(details['startTime'], "%H:%M")
            end_dt = start_dt + timedelta(minutes=30)
            details['endTime'] = end_dt.strftime("%H:%M")
        except:
            pass
    
    return details if details else None

def parse_payment_details(text: str) -> dict:
    """Parse payment details from text"""
    patterns = {
        "amount": r"amount[:\s]*([^\n]+)",
        "appointmentId": r"appointment id[:\s]*([^\n]+)"
    }
    
    details = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            details[key] = match.group(1).strip()
    
    return details if details else None

# API Endpoints
@app.route('/api/ai-appointment', methods=['GET', 'POST', 'OPTIONS'])
def handle_ai_appointment():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
        
    if request.method == 'GET':
        return jsonify({
            "status": "active",
            "service": "AI Appointment Booking",
            "version": "1.0",
            "documentation": "Use POST method to interact with the AI assistant"
        }), 200

    # POST request handling
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        user_message = data.get('message', '')
        conversation_history = data.get('history', [])
        patient_info = data.get('patient_info', {})
        
        if not user_message:
            return jsonify({"error": "Message is required"}), 400
        
        specialties = get_specialties()
        doctors_info = "\n".join(
            f"{spec}: {', '.join(d['name'] for d in get_doctors_by_specialty(spec))}"
            for spec in specialties
        )
        
        system_prompt = SYSTEM_PROMPT + f"\n\nAvailable specialties: {', '.join(specialties)}\nAvailable doctors:\n{doctors_info}"
        
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})
        
        # Call Groq API
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            messages=messages,
            model="llama3-70b-8192",
            temperature=0.7
        )
        
        ai_response = response.choices[0].message.content
        
        action = None
        if "[BOOK_APPOINTMENT]" in ai_response:
            appointment_details = parse_appointment_details(ai_response)
            if appointment_details:
                booking_result = book_appointment(patient_info, appointment_details)
                if booking_result.get('success'):
                    action = {
                        "type": "appointment_booked",
                        "details": booking_result
                    }
                    ai_response = f"Appointment booked successfully! Please verify your payment with this OTP: {booking_result['otp']}"
                else:
                    ai_response = booking_result.get('error', "Failed to book appointment. Please try again.")
        
        if "[REQUEST_PAYMENT]" in ai_response:
            payment_details = parse_payment_details(ai_response)
            if payment_details:
                action = {
                    "type": "payment_request",
                    "details": payment_details
                }
                ai_response = ai_response.replace("[REQUEST_PAYMENT]", "")
        
        audio_base64 = text_to_speech(ai_response)
        
        return jsonify({
            "text_response": ai_response,
            "audio_response": audio_base64,
            "action": action
        })
    
    except Exception as e:
        logger.error(f"Error in AI appointment handler: {str(e)}")
        return jsonify({
            "error": "Our servers are busy right now. Please try again later.",
            "details": str(e)
        }), 500

@app.route('/api/process-payment', methods=['POST', 'OPTIONS'])
def handle_payment():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
        
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "error": "Missing required fields. Please provide all payment details."
            }), 400
        
        patient_email = data.get('patient_email')
        amount = float(data.get('amount', 0))
        appointment_id = data.get('appointment_id')
        otp = data.get('otp')
        
        if not all([patient_email, amount, appointment_id, otp]):
            return jsonify({
                "success": False,
                "error": "Missing required fields. Please provide all payment details."
            }), 400
        
        result = process_payment(patient_email, amount, appointment_id, otp)
        
        if result.get('success'):
            return jsonify({
                "success": True,
                "newBalance": result['newBalance'],
                "transactionId": result['transactionId'],
                "message": result['message']
            })
        else:
            return jsonify({
                "success": False,
                "error": result.get('error', "Payment failed. Please try again.")
            }), 400
    
    except Exception as e:
        logger.error(f"Error in payment handler: {str(e)}")
        return jsonify({
            "success": False,
            "error": "Payment processing is currently unavailable. Please try again later."
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
