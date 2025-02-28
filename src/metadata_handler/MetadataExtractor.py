from pypdf import PdfReader
import os

class MetadataExtractor:
    def __init__(self, file_path: str):
        self._validate(file_path)
        self._file_path = file_path
        self._load_document()

    def extract_basic_metadata(self):
        # Get metadata
        metadata = self._document.metadata

        # Display metadata
        for key, value in metadata.items():
            print(f"{key}: {value}")

    def extract_toc_metadata(self, file_path: str):
        pass

    def extract_ocr_metadata(self, file_path: str):
        pass
    
    def _load_document(self):
        self._document = PdfReader(self._file_path)
        return self._document
    
    def _validate(self, document_path: str):
        assert os.path.exists(
            document_path
        ), f"Document path did not found: {document_path}"