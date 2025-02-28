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
        
        return metadata
  
    def _load_document(self):
        self._document = PdfReader(self._file_path)
        return self._document
    
    def _validate(self, document_path: str):
        assert os.path.exists(
            document_path
        ), f"Document path did not found: {document_path}"

    def contains_ocr(self):
        # Check if the document contains any text
        for page in self._document.pages:
            if page.extract_text():
                return True
        return False

    def contains_toc(self):
        # Check for bookmarks or specific patterns that indicate a TOC
        if self._document.outlines:
            return True
        # Additional logic to detect TOC patterns can be added here
        return False