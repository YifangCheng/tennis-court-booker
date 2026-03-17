from abc import ABC, abstractmethod

from shared.runtime import RunOptions


class BookingSite(ABC):
    name: str
    description: str

    @abstractmethod
    async def run(self, options: RunOptions) -> None:
        raise NotImplementedError
