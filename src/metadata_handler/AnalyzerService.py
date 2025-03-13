from metadata_handler.MetadataExtractor import MetadataExtractor
from metadata_handler.MetadataProcessor import MetadataProcessor


class AnalyzerService:
    _instance = None
    metadata_processor: MetadataProcessor
    metadata_extractor: MetadataExtractor

    def __init__(self, document_path: str):
        if not hasattr(self, "initialized"):
            self.metadata_extractor = MetadataExtractor(document_path)
            self.metadata_processor = MetadataProcessor()
            self.initialized = True
        else:
            return self._instance

    def analyze_metadata(self):
        file_type = self.metadata_extractor.extract_file_type()
        extracted_metadata = self.metadata_extractor.extract_basic_metadata()
        # processed_metadata = self.metadata_processor.process_metadata(extracted_metadata)
        has_ocr = self.metadata_extractor.contains_ocr()
        toc = self.metadata_extractor.extract_toc()
        return {
            "file_type": file_type, # if not PDF -> pass
            "processed_metadata": extracted_metadata,
            "toc": toc, # TOC in nested dict format if exists, else None
            "has_ocr": has_ocr
        }
