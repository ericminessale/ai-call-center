"""Contacts API - CRUD operations for customer/caller contacts."""

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import db
from app.models import Contact, Call

contacts_bp = Blueprint('contacts', __name__)


@contacts_bp.route('', methods=['GET'])
@jwt_required()
def list_contacts():
    """List contacts with optional search and pagination."""
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    sort_by = request.args.get('sort_by', 'last_interaction')  # last_interaction, name, created
    include_blocked = request.args.get('include_blocked', 'false').lower() == 'true'

    query = Contact.query

    # Filter blocked
    if not include_blocked:
        query = query.filter(Contact.is_blocked == False)

    # Search
    if search:
        search_term = f'%{search}%'
        query = query.filter(
            db.or_(
                Contact.first_name.ilike(search_term),
                Contact.last_name.ilike(search_term),
                Contact.display_name.ilike(search_term),
                Contact.phone.ilike(search_term),
                Contact.email.ilike(search_term),
                Contact.company.ilike(search_term),
            )
        )

    # Sort
    if sort_by == 'name':
        query = query.order_by(Contact.display_name.asc().nullslast(), Contact.first_name.asc().nullslast())
    elif sort_by == 'created':
        query = query.order_by(Contact.created_at.desc())
    else:  # last_interaction
        query = query.order_by(Contact.last_interaction_at.desc().nullslast())

    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'contacts': [c.to_dict_minimal() for c in pagination.items],
        'total': pagination.total,
        'page': pagination.page,
        'pages': pagination.pages,
        'hasNext': pagination.has_next,
        'hasPrev': pagination.has_prev,
    })


@contacts_bp.route('/<int:contact_id>', methods=['GET'])
@jwt_required()
def get_contact(contact_id):
    """Get a single contact with full details."""
    contact = Contact.query.get_or_404(contact_id)
    return jsonify(contact.to_dict(include_stats=True))


@contacts_bp.route('', methods=['POST'])
@jwt_required()
def create_contact():
    """Create a new contact."""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    phone = data.get('phone')
    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400

    # Check if contact already exists
    normalized_phone = Contact.normalize_phone(phone)
    existing = Contact.find_by_phone(normalized_phone)
    if existing:
        return jsonify({'error': 'Contact with this phone number already exists', 'contact': existing.to_dict()}), 409

    contact = Contact(
        phone=normalized_phone,
        first_name=data.get('firstName'),
        last_name=data.get('lastName'),
        display_name=data.get('displayName'),
        email=data.get('email'),
        company=data.get('company'),
        job_title=data.get('jobTitle'),
        account_tier=data.get('accountTier', 'prospect'),
        account_status=data.get('accountStatus', 'active'),
        external_id=data.get('externalId'),
        is_vip=data.get('isVip', False),
        notes=data.get('notes'),
    )

    if data.get('tags'):
        contact.tags_list = data.get('tags')

    if data.get('customFields'):
        contact.custom_fields_dict = data.get('customFields')

    db.session.add(contact)
    db.session.commit()

    return jsonify(contact.to_dict()), 201


@contacts_bp.route('/<int:contact_id>', methods=['PUT'])
@jwt_required()
def update_contact(contact_id):
    """Update an existing contact."""
    contact = Contact.query.get_or_404(contact_id)
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Update fields if provided
    if 'firstName' in data:
        contact.first_name = data['firstName']
    if 'lastName' in data:
        contact.last_name = data['lastName']
    if 'displayName' in data:
        contact.display_name = data['displayName']
    if 'email' in data:
        contact.email = data['email']
    if 'company' in data:
        contact.company = data['company']
    if 'jobTitle' in data:
        contact.job_title = data['jobTitle']
    if 'accountTier' in data:
        contact.account_tier = data['accountTier']
    if 'accountStatus' in data:
        contact.account_status = data['accountStatus']
    if 'externalId' in data:
        contact.external_id = data['externalId']
    if 'isVip' in data:
        contact.is_vip = data['isVip']
    if 'isBlocked' in data:
        contact.is_blocked = data['isBlocked']
    if 'notes' in data:
        contact.notes = data['notes']
    if 'tags' in data:
        contact.tags_list = data['tags']
    if 'customFields' in data:
        contact.custom_fields_dict = data['customFields']

    # Phone number change requires uniqueness check
    if 'phone' in data:
        new_phone = Contact.normalize_phone(data['phone'])
        if new_phone != contact.phone:
            existing = Contact.find_by_phone(new_phone)
            if existing:
                return jsonify({'error': 'Phone number already in use by another contact'}), 409
            contact.phone = new_phone

    db.session.commit()
    return jsonify(contact.to_dict())


@contacts_bp.route('/<int:contact_id>', methods=['DELETE'])
@jwt_required()
def delete_contact(contact_id):
    """Delete a contact."""
    contact = Contact.query.get_or_404(contact_id)

    # Soft delete by blocking, or hard delete based on preference
    # Using hard delete for now
    db.session.delete(contact)
    db.session.commit()

    return jsonify({'message': 'Contact deleted'}), 200


@contacts_bp.route('/lookup', methods=['GET'])
@jwt_required()
def lookup_contact():
    """Look up a contact by phone number."""
    phone = request.args.get('phone')
    if not phone:
        return jsonify({'error': 'Phone number required'}), 400

    contact = Contact.find_by_phone(phone)
    if contact:
        return jsonify(contact.to_dict())
    else:
        return jsonify({'contact': None, 'found': False}), 200


@contacts_bp.route('/lookup-or-create', methods=['POST'])
@jwt_required()
def lookup_or_create_contact():
    """Look up a contact by phone, or create if not found."""
    data = request.get_json()
    phone = data.get('phone')

    if not phone:
        return jsonify({'error': 'Phone number required'}), 400

    contact = Contact.find_by_phone(phone)
    created = False

    if not contact:
        contact = Contact(
            phone=Contact.normalize_phone(phone),
            first_name=data.get('firstName'),
            last_name=data.get('lastName'),
            display_name=data.get('displayName'),
            company=data.get('company'),
        )
        db.session.add(contact)
        db.session.commit()
        created = True

    return jsonify({
        'contact': contact.to_dict(),
        'created': created
    }), 201 if created else 200


@contacts_bp.route('/<int:contact_id>/interactions', methods=['GET'])
@jwt_required()
def get_contact_interactions(contact_id):
    """Get all interactions (calls) for a contact."""
    contact = Contact.query.get_or_404(contact_id)

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    pagination = contact.calls.order_by(Call.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify({
        'interactions': [c.to_dict() for c in pagination.items],
        'total': pagination.total,
        'page': pagination.page,
        'pages': pagination.pages,
    })


@contacts_bp.route('/<int:contact_id>/stats', methods=['GET'])
@jwt_required()
def get_contact_stats(contact_id):
    """Get computed statistics for a contact."""
    contact = Contact.query.get_or_404(contact_id)

    # Update stats before returning
    contact.update_stats()
    db.session.commit()

    return jsonify({
        'totalCalls': contact.total_calls,
        'lastInteractionAt': contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
        'averageSentiment': contact.average_sentiment,
    })


@contacts_bp.route('/recent', methods=['GET'])
@jwt_required()
def get_recent_contacts():
    """Get recently interacted contacts (for quick access)."""
    limit = request.args.get('limit', 10, type=int)

    contacts = Contact.query.filter(
        Contact.last_interaction_at.isnot(None),
        Contact.is_blocked == False
    ).order_by(
        Contact.last_interaction_at.desc()
    ).limit(limit).all()

    return jsonify({
        'contacts': [c.to_dict_minimal() for c in contacts]
    })


@contacts_bp.route('/active', methods=['GET'])
@jwt_required()
def get_contacts_with_active_calls():
    """Get contacts that currently have active calls."""
    active_statuses = ['ringing', 'in-progress', 'initiated', 'ai_active']

    contacts = Contact.query.join(Call).filter(
        Call.status.in_(active_statuses)
    ).distinct().all()

    # Include active call info
    result = []
    for contact in contacts:
        contact_data = contact.to_dict_minimal()
        active_call = contact.calls.filter(Call.status.in_(active_statuses)).first()
        if active_call:
            contact_data['activeCall'] = active_call.to_dict()
        result.append(contact_data)

    return jsonify({'contacts': result})
