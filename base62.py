import string

alphabet=string.ascii_letters+string.digits

BASE=len(alphabet)#base 62 encoding

def encode(num: int)->str:
    """Converts a database integer ID into a base 62 string
    """
    if num < 0:
        raise ValueError("Number must be non-negative")
    
    if(num==0):
        return alphabet[0]
    
    result=[]
    while num>0:
        remainder=num%BASE
        result.append(alphabet[remainder])
        num//=BASE
    
    return "".join(reversed(result)) #reverse the result

def decode(short_url: str)->int:
    """Converts a base 62 string back to an integer
    """
    num=0
    for char in short_url:
        num=num*BASE + alphabet.index(char)
    return num 