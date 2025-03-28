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

app = Flask(_name_)
CORS(app)
# Replace this line:

# With this to allow your Vercel frontend:
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "https://your-vercel-app.vercel.app",  # Replace with your actual Vercel URL
            "http://localhost:3000"
        ],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})
# Initialize Firebase
cred = credentials.Certificate("medicare11-firebase-adminsdk-fbsvc-8c856c8938.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Initialize APIs
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

def text_to_speech(text: str) -> str:
    """Convert text to speech using ElevenLabs API"""
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
    if response.status_code == 200:
        return base64.b64encode(response.content).decode('utf-8')
    raise Exception(f"ElevenLabs API error: {response.text}")

def get_specialties() -> list:
    """Get all available specialties from Firebase"""
    docs = db.collection('doctors').stream()
    specialties = set()
    for doc in docs:
        specialties.add(doc.to_dict().get('specialty', ''))
    return list(specialties)

def get_doctors_by_specialty(specialty: str) -> list:
    """Get doctors by specialty from Firebase"""
    docs = db.collection('doctors').where('specialty', '==', specialty).stream()
    return [{'id': doc.id, **doc.to_dict()} for doc in docs]

def check_doctor_availability(doctor_id: str, date: str, time: str) -> bool:
    """Check if doctor is available at given date/time"""
    start_time = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_time = start_time + timedelta(minutes=30)
    
    time_slots_ref = db.collection('timeSlots').where('doctorId', '==', doctor_id) \
        .where('date', '==', date) \
        .where('startTime', '==', time) \
        .where('isBooked', '==', False) \
        .limit(1).stream()
    
    return len(list(time_slots_ref)) > 0

def get_patient_by_email(email: str) -> dict:
    """Get patient document by email"""
    docs = db.collection('patients').where('email', '==', email).limit(1).stream()
    for doc in docs:
        return {'id': doc.id, **doc.to_dict()}
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
        
        time_slots_ref = db.collection('timeSlots').where('doctorId', '==', appointment_details['doctorId']) \
            .where('date', '==', appointment_details['date']) \
            .where('startTime', '==', appointment_details['startTime']) \
            .limit(1).stream()
        
        for slot in time_slots_ref:
            db.collection('timeSlots').document(slot.id).update({'isBooked': True})
        
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
        return {
            'success': False,
            'error': f"Payment processing failed: {str(e)}"
        }

@app.route('/api/ai-appointment', methods=['GET', 'POST', 'OPTIONS'])
def handle_ai_appointment():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
        
    if request.method == 'GET':
        return jsonify({
            "status": "active",
            "service": "AI Appointment Booking",
            "version": "1.0"
        }), 200

    # Existing POST handling code
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        user_message = data.get('message', '')
        conversation_history = data.get('history', [])
        patient_info = data.get('patient_info', {})
        
        # [Rest of your existing POST handling code...]
        
    except Exception as e:
        app.logger.error(f"Error in AI appointment: {str(e)}")
        return jsonify({
            "error": "Our servers are busy right now. Please try again later.",
            "details": str(e)
        }), 500



@app.route('/api/process-payment', methods=['POST'])
def handle_payment():
    try:
        data = request.json
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
        return jsonify({
            "success": False,
            "error": "Payment processing is currently unavailable. Please try again later."
        }), 500

def parse_appointment_details(text: str) -> dict:
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

if _name_ == '_main_':
    app.run(host='0.0.0.0', port=5000, debug=True)
