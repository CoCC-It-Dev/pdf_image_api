# main.py - API Principal
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import List, Optional
import base64
import io
import os
import tempfile
import logging
from reportlab.pdfgen import canvas
from PyPDF2 import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
import uuid
import asyncio
from contextlib import asynccontextmanager
import json

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Modelos Pydantic
class ImageData(BaseModel):
    image_base64: str = Field(..., description="Imagen en formato base64")
    x_position: float = Field(..., description="Posición X en puntos", ge=0)
    y_position: float = Field(..., description="Posición Y en puntos", ge=0)
    width: float = Field(..., description="Ancho de la imagen en puntos", gt=0)
    height: float = Field(..., description="Alto de la imagen en puntos", gt=0)
    page_number: int = Field(1, description="Número de página (empezando en 1)", ge=1)
    
    @validator('image_base64')
    def validate_base64_image(cls, v):
        if not validate_base64_image(v):
            raise ValueError('La imagen base64 no es válida')
        return v

class MultipleImagesRequest(BaseModel):
    images: List[ImageData] = Field(..., description="Array de imágenes a insertar", min_items=1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestión del ciclo de vida de la aplicación"""
    logger.info("Iniciando API PDF...")
    yield
    logger.info("Cerrando API PDF...")

app = FastAPI(
    title="PDF Multiple Images Inserter API",
    version="2.0.0",
    description="API para insertar múltiples imágenes en archivos PDF",
    lifespan=lifespan
)

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def validate_base64_image(base64_string: str) -> bool:
    """Valida si una cadena base64 es una imagen válida"""
    try:
        if base64_string.startswith('data:image'):
            # Remover el prefijo data:image/...;base64,
            base64_string = base64_string.split(',')[1]
        
        image_data = base64.b64decode(base64_string)
        
        # Verificar que tenga un tamaño mínimo razonable
        if len(image_data) < 100:
            return False
            
        # Verificar headers de imagen comunes
        image_headers = [
            b'\xff\xd8\xff',  # JPEG
            b'\x89PNG\r\n\x1a\n',  # PNG
            b'GIF87a',  # GIF87a
            b'GIF89a',  # GIF89a
            b'BM',  # BMP
        ]
        
        return any(image_data.startswith(header) for header in image_headers)
    except Exception:
        return False

def process_base64_image(image_base64: str) -> ImageReader:
    """Procesa una imagen base64 y devuelve un ImageReader"""
    try:
        # Limpiar el base64 si tiene prefijo data:image
        if image_base64.startswith('data:image'):
            image_base64 = image_base64.split(',')[1]
        
        image_data = base64.b64decode(image_base64)
        return ImageReader(io.BytesIO(image_data))
    except Exception as e:
        logger.error(f"Error decodificando imagen: {e}")
        raise HTTPException(status_code=400, detail="Error al procesar la imagen base64")

@app.post("/insert-images/")
async def insert_multiple_images_in_pdf(
    pdf_file: UploadFile = File(..., description="Archivo PDF"),
    images_data: str = Body(..., description="JSON con array de imágenes y sus propiedades")
):
    """
    Inserta múltiples imágenes en base64 en un PDF en las posiciones especificadas
    """
    
    # Validaciones de entrada
    if not pdf_file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")
    
    # Parsear JSON de imágenes
    try:
        images_json = json.loads(images_data)
        request_data = MultipleImagesRequest(**images_json)
        images = request_data.images
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON de imágenes inválido")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Datos de imagen inválidos: {str(e)}")
    
    temp_files = []  # Para limpiar archivos temporales
    
    try:
        # Leer el PDF original
        pdf_content = await pdf_file.read()
        pdf_reader = PdfReader(io.BytesIO(pdf_content))
        total_pages = len(pdf_reader.pages)
        
        # Validar números de página y dimensiones
        for i, img in enumerate(images):
            if img.page_number > total_pages:
                raise HTTPException(
                    status_code=400,
                    detail=f"Imagen {i+1}: Número de página inválido. El PDF tiene {total_pages} páginas"
                )
            
            # Validar dimensiones de la página
            page = pdf_reader.pages[img.page_number - 1]
            page_width = float(page.mediabox.width)
            page_height = float(page.mediabox.height)
            
            if img.x_position + img.width > page_width:
                raise HTTPException(
                    status_code=400,
                    detail=f"Imagen {i+1}: Se sale del ancho de la página (máximo: {page_width} puntos)"
                )
                
            if img.y_position + img.height > page_height:
                raise HTTPException(
                    status_code=400,
                    detail=f"Imagen {i+1}: Se sale del alto de la página (máximo: {page_height} puntos)"
                )
        
        # Agrupar imágenes por página
        images_by_page = {}
        for img in images:
            if img.page_number not in images_by_page:
                images_by_page[img.page_number] = []
            images_by_page[img.page_number].append(img)
        
        # Crear overlays para cada página que necesite imágenes
        overlay_files = {}
        
        for page_num, page_images in images_by_page.items():
            # Obtener dimensiones de la página
            page = pdf_reader.pages[page_num - 1]
            page_width = float(page.mediabox.width)
            page_height = float(page.mediabox.height)
            
            # Crear PDF temporal con las imágenes de esta página
            temp_overlay = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
            temp_files.append(temp_overlay.name)
            temp_overlay.close()
            
            # Crear canvas con las dimensiones de la página original
            c = canvas.Canvas(temp_overlay.name, pagesize=(page_width, page_height))
            
            # Insertar todas las imágenes de esta página
            for img in page_images:
                try:
                    image_reader = process_base64_image(img.image_base64)
                    # Insertar imagen (ReportLab usa origen en esquina inferior izquierda)
                    c.drawImage(
                        image_reader,
                        img.x_position,
                        page_height - img.y_position - img.height,  # Ajustar coordenada Y
                        width=img.width,
                        height=img.height,
                        mask='auto'
                    )
                except Exception as e:
                    logger.error(f"Error procesando imagen: {e}")
                    raise HTTPException(
                        status_code=400,
                        detail=f"Error procesando imagen en página {page_num}: {str(e)}"
                    )
            
            c.save()
            overlay_files[page_num] = temp_overlay.name
        
        # Combinar PDFs
        pdf_writer = PdfWriter()
        
        for i, page in enumerate(pdf_reader.pages):
            page_number = i + 1
            
            if page_number in overlay_files:
                # Combinar página original con overlay
                overlay_reader = PdfReader(overlay_files[page_number])
                page.merge_page(overlay_reader.pages[0])
            
            pdf_writer.add_page(page)
        
        # Crear archivo de salida
        output_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        temp_files.append(output_file.name)
        
        with open(output_file.name, 'wb') as f:
            pdf_writer.write(f)
        
        logger.info(f"PDF procesado exitosamente: {pdf_file.filename} con {len(images)} imágenes")
        
        # Devolver archivo
        return FileResponse(
            path=output_file.name,
            filename=f"modified_{pdf_file.filename}",
            media_type="application/pdf",
            background=lambda: cleanup_files(temp_files)
        )
        
    except HTTPException:
        cleanup_files(temp_files)
        raise
    except Exception as e:
        cleanup_files(temp_files)
        logger.error(f"Error inesperado: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")

def cleanup_files(file_paths: list):
    """Limpia archivos temporales"""
    for file_path in file_paths:
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
        except Exception as e:
            logger.warning(f"No se pudo eliminar archivo temporal {file_path}: {e}")

@app.get("/")
async def root():
    """Endpoint de información"""
    return {
        "message": "API para insertar múltiples imágenes en PDFs",
        "version": "2.0.0",
        "endpoints": {
            "/insert-images/": "POST - Insertar múltiples imágenes en PDF",
            "/docs": "Documentación interactiva",
            "/health": "Estado de la API"
        },
        "usage": {
            "coordinates": "Coordenadas en puntos (72 puntos = 1 pulgada)",
            "origin": "Origen (0,0) en esquina superior izquierda",
            "supported_formats": ["JPEG", "PNG", "GIF", "BMP"],
            "example_request": {
                "images": [
                    {
                        "image_base64": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg==",
                        "x_position": 100,
                        "y_position": 100,
                        "width": 50,
                        "height": 50,
                        "page_number": 1
                    }
                ]
            }
        }
    }

@app.get("/health")
async def health_check():
    """Verificación de estado"""
    return {
        "status": "healthy",
        "message": "API funcionando correctamente"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0", 
        port=8000,
        reload=True,
        access_log=True
    )