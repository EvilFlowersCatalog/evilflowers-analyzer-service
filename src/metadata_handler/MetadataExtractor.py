import fitz
# from pypdf import PdfReader
import os

class MetadataExtractor:
    def __init__(self, file_path: str):
        self._validate(file_path)
        self._file_path = file_path
        self._load_document()

    def extract_file_type(self):
        _, extension = os.path.splitext(os.path.basename(self._file_path))
        if extension:
            return extension[1:] 
        return "unknown"

    def extract_basic_metadata(self):
        # Get metadata
        metadata = self._document.metadata        
        return metadata
  
    def _load_document(self):
        self._document = fitz.open(self._file_path)
        return self._document
    
    def _validate(self, document_path: str):
        assert os.path.exists(
            document_path
        ), f"Document path did not found: {document_path}"

    def contains_ocr(self):
        # Check if the document contains any text
        '''for page in self._document.pages:
            if page.extract_text():
                return True
        return False'''

    def extract_toc(self):
        # Check for bookmarks or specific patterns that indicate a TOC
        toc = self._document.get_toc()
        if toc:
            return self._toc_to_dict(toc)
        return None
    
    def _toc_to_dict(self, toc):
        toc_dict = {}
        current_level = {0: toc_dict} 
     
        for entry in toc:
            level, title, page = entry
            entry_dict = {"page": page}  
            
            if level == 1:
                toc_dict[title] = entry_dict
                current_level[1] = entry_dict
            else:
                parent_level = current_level[level - 1]
                if "children" not in parent_level:
                    parent_level["children"] = {}
                parent_level["children"][title] = entry_dict
                current_level[level] = entry_dict
        
        return toc_dict