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
        extracted_metadata = self.metadata_extractor.extract_basic_metadata()
        processed_metadata = self.metadata_processor.process_metadata(extracted_metadata)
        return processed_metadata
