#  D:/bynry/.venv/Scripts/python.exe app.py
from flask import Flask
from flask import request, jsonify, current_app
from flask_sqlalchemy import SQLAlchemy
from decimal import Decimal, InvalidOperation
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from datetime import datetime, timedelta
import math
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}/{os.getenv('DB_NAME')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Models
class Company(db.Model):
    __tablename__ = 'company'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

class Warehouse(db.Model):
    __tablename__ = 'warehouse'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)

class Product(db.Model):
    __tablename__ = 'product'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    sku = db.Column(db.String(100), nullable=False, unique=True)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    reorder_threshold = db.Column(db.Integer, nullable=False, default=10)

class Supplier(db.Model):
    __tablename__ = 'supplier'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    contact_email = db.Column(db.String(255))

class ProductSupplier(db.Model):
    __tablename__ = 'product_supplier'
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), primary_key=True)
    is_primary = db.Column(db.Boolean, default=False)

class Inventory(db.Model):
    __tablename__ = 'inventory'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)

class InventoryHistory(db.Model):
    __tablename__ = 'inventory_history'
    id = db.Column(db.Integer, primary_key=True)
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'), nullable=False)
    change_amount = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# create new product endpoint
@app.route('/api/products', methods=['POST'])
def create_product():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid or missing JSON body'}), 400

    name = data.get('name')
    sku = data.get('sku')
    price_raw = data.get('price')

    if not name or not sku or price_raw is None:
        return jsonify({'error': 'Fields name, sku and price are required'}), 400

    warehouse_id = data.get('warehouse_id')  
    initial_quantity = data.get('initial_quantity', 0)

    try:
        price = Decimal(str(price_raw)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        return jsonify({'error': 'price must be a decimal value'}), 400

    try:
        qty = int(initial_quantity)
        if qty < 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'initial_quantity must be a non-negative integer'}), 400

# Normalize SKU to avoid case-sensitive duplicates
    normalized_sku = sku.strip().upper()
    existing_product = Product.query.filter_by(sku=normalized_sku).first()
    if existing_product:
        return jsonify({"error": "SKU already exists"}), 409

    # simple 'warehouse exist' check
    if warehouse_id is not None:
        if not Warehouse.query.get(warehouse_id):
            return jsonify({'error': 'warehouse_id does not exist'}), 400

    try:
        with db.session.begin():
            product = Product(
                name=name.strip(),
                sku=normalized_sku, 
                price=price
            )
            db.session.add(product)
            db.session.flush() 

            if warehouse_id is not None:
                inventory = Inventory(
                    product_id=product.id,
                    warehouse_id=warehouse_id,
                    quantity=qty
                )
                db.session.add(inventory)
                db.session.flush() 

                # Add inventory history record for initial stock
                if qty > 0:
                    history = InventoryHistory(
                        inventory_id=inventory.id,
                        change_amount=qty,
                        reason="Initial stock"
                    )
                    db.session.add(history)

        return jsonify({"message": "Product created", "product_id": product.id}), 201

    except IntegrityError as ie:
        db.session.rollback()
        current_app.logger.exception("IntegrityError creating product")
        return jsonify({"error": "SKU already exists or integrity constraint violated"}), 409

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unexpected error creating product")
        return jsonify({"error": "Internal server error"}), 500

# company low stock alerts endpoint
@app.route('/api/companies/<int:company_id>/alerts/low-stock', methods=['GET'])
def get_low_stock_alerts(company_id):
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    recent_sales_subquery = db.session.query(
        InventoryHistory.inventory_id
    ).filter(
        InventoryHistory.created_at >= thirty_days_ago,
        InventoryHistory.change_amount < 0
    ).distinct().subquery()

    low_stock_items = db.session.query(
        Product,
        Inventory.quantity,
        Warehouse,
        Supplier,
        Inventory.id.label('inventory_id')
    ).join(
        Product, Inventory.product_id == Product.id
    ).join(
        Warehouse, Inventory.warehouse_id == Warehouse.id
    ).join(
        recent_sales_subquery, Inventory.id == recent_sales_subquery.c.inventory_id
    ).outerjoin(
        ProductSupplier, (Product.id == ProductSupplier.product_id) & (ProductSupplier.is_primary == True)
    ).outerjoin(
        Supplier, ProductSupplier.supplier_id == Supplier.id
    ).filter(
        Warehouse.company_id == company_id,
        Inventory.quantity < Product.reorder_threshold  
    ).all()

    alerts = []
    for product, current_stock, warehouse, supplier, inventory_id in low_stock_items:
        usage_data = db.session.query(
            func.sum(InventoryHistory.change_amount * -1).label('total_usage') 
        ).filter(
            InventoryHistory.inventory_id == inventory_id,
            InventoryHistory.created_at >= thirty_days_ago,
            InventoryHistory.change_amount < 0 
        ).first()
        
        # Calculate days until stockout
        days_until_stockout = None
        if usage_data and usage_data.total_usage:
            avg_daily_usage = usage_data.total_usage / Decimal(30)  
            if avg_daily_usage > 0:
               days_until_stockout = int(current_stock / avg_daily_usage)
        
        supplier_info = None
        if supplier:
            supplier_info = { "id": supplier.id, "name": supplier.name, "contact_email": supplier.contact_email }
        
        alerts.append({
            "product_id": product.id, 
            "product_name": product.name, 
            "sku": product.sku,
            "warehouse_id": warehouse.id, 
            "warehouse_name": warehouse.name,
            "current_stock": current_stock, 
            "threshold": product.reorder_threshold,  
            "days_until_stockout": days_until_stockout, 
            "supplier": supplier_info
        })

    return jsonify({"alerts": alerts, "total_alerts": len(alerts)})

if __name__ == "__main__":
    app.run(debug=True)



